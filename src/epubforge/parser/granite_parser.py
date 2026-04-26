"""Stage 1 — Granite-Docling-258M VLM parser via llama-server (OpenAI-compatible API).

This module is the secondary "VLM" parse pipeline that runs alongside the
standard Docling+OCR pipeline (see ``docling_parser.py``). It is off by default
and only invoked when ``settings.enabled`` is True at the orchestration layer
(I3 wires this into the pipeline; this module provides the function only).

Operational requirements (see ``docs/explorations/granite-llama-server-spike.md``):

- Requires llama-server started with the ``--special`` flag; without it,
  ``<doctag>`` / ``<text>`` etc. are emitted as plain tokens, the doctags
  parser fails silently, and markdown export comes back empty.
- Requires ``--jinja`` for chat-template-based VLM input.
- Only validated with concurrency=1 on 8GB WSL2 hardware.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class GraniteParseResult:
    """Outcome of a single ``parse_pdf_granite`` invocation.

    ``successful_pages``/``failed_pages`` are 1-based page numbers within the
    source PDF that the caller asked us to convert.
    """

    successful_pages: list[int]
    failed_pages: list[int]
    elapsed_seconds: float
    out_path: Path
    page_count: int
    manifest_path: Path | None = None
    repeated_line_warnings: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _derive_models_url(api_url: str) -> str:
    """Derive ``<base>/models`` from a chat-completions URL.

    >>> _derive_models_url("http://localhost:8080/v1/chat/completions")
    'http://localhost:8080/v1/models'
    >>> _derive_models_url("http://localhost:8080/v1")
    'http://localhost:8080/v1/models'
    """
    base = api_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base.rstrip("/") + "/models"


def _health_check(api_url: str) -> None:
    """Verify llama-server is reachable; raise ``RuntimeError`` if not.

    The check uses a 5-second GET on ``<base>/models``. Failure is treated as
    fatal — we want to fail fast before triggering an expensive PDF
    rasterisation that is guaranteed to error per-page.
    """
    models_url = _derive_models_url(api_url)
    log.info("granite: health check GET %s", models_url)
    try:
        resp = httpx.get(models_url, timeout=5)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — we re-raise as RuntimeError
        raise RuntimeError(
            f"Granite llama-server not reachable at {models_url}; "
            f"start it first (see docs/explorations/granite-llama-server-spike.md). "
            f"Original error: {exc!r}"
        ) from exc
    log.info("granite: health check passed (HTTP %d)", resp.status_code)


# ---------------------------------------------------------------------------
# Converter construction
# ---------------------------------------------------------------------------


def _build_converter(settings: Any) -> Any:
    """Build the Docling DocumentConverter backed by ApiVlmOptions.

    ``settings`` is a ``GraniteSettings`` instance, kept loose-typed here to
    keep this module importable without forcing the rest of the package to
    pull in ``epubforge.config`` at import time.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmPipelineOptions
    from docling.datamodel.pipeline_options_vlm_model import (
        ApiVlmOptions,
        ResponseFormat,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    # ApiVlmOptions has a top-level ``temperature`` field; we pin it to 0.0
    # for reproducibility. ``params`` carries arbitrary OpenAI-compatible
    # request parameters (spike used model + max_tokens here).
    api_opts = ApiVlmOptions(
        url=settings.api_url,
        params={"model": settings.api_model, "max_tokens": settings.max_tokens},
        prompt=settings.prompt,
        response_format=ResponseFormat.DOCTAGS,
        scale=settings.scale,
        timeout=settings.timeout_seconds,
        concurrency=settings.concurrency,
        temperature=0.0,
    )
    pipe_opts = VlmPipelineOptions(
        enable_remote_services=True, vlm_options=api_opts
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline, pipeline_options=pipe_opts
            )
        }
    )


# ---------------------------------------------------------------------------
# Doctags hygiene
# ---------------------------------------------------------------------------


_LOC_RE = re.compile(r"<loc_\d+>")
_TEXT_BLOCK_RE = re.compile(r"<text>(?P<body>.*?)</text>", re.DOTALL)


def _text_signature(line: str) -> str | None:
    """Return inner content of a ``<text>...</text>`` element with loc tags
    stripped, or ``None`` if the line is not a single ``<text>`` element.

    Used as the equality key for the repeated-line dedup pass.
    """
    m = _TEXT_BLOCK_RE.fullmatch(line.strip())
    if not m:
        return None
    body = m.group("body")
    body = _LOC_RE.sub("", body)
    return body.strip()


def _detect_repeated_lines(doctags: str, *, threshold: int = 3) -> tuple[str, int]:
    """Collapse runs of identical ``<text>`` elements into a single line.

    Defends against VLM hallucination on decorative pages — see spike report
    page_004 case where the model emitted ``<text>...吴心越著...</text>`` 14
    times in a row with only the bbox locs varying. Equality is computed on
    the inner text content (loc tags stripped), so "decorative repetition
    with shifted bboxes" is detected as repetition.

    Returns the (possibly-modified) doctags string and the number of
    surplus lines dropped (0 if nothing was collapsed).

    Args:
        doctags: per-page doctags string from ``DoclingDocument.export_to_doctags``.
        threshold: minimum run length that counts as "repetition" (default 3).
    """
    lines = doctags.split("\n")
    out: list[str] = []
    dropped = 0

    last_sig: str | None = None
    run_len = 0  # how many *identical* lines have been seen in the current run

    for line in lines:
        sig = _text_signature(line)
        if sig is not None and sig != "" and sig == last_sig:
            run_len += 1
            if run_len >= threshold:
                # Drop this surplus line; warn exactly once when we cross
                # the threshold for transparency in logs.
                if run_len == threshold:
                    log.warning(
                        "granite: repeated <text> run detected (%d so far), "
                        "collapsing surplus copies: %r",
                        run_len,
                        sig[:80],
                    )
                dropped += 1
                continue
            out.append(line)
        else:
            last_sig = sig
            run_len = 1 if sig is not None else 0
            out.append(line)

    return "\n".join(out), dropped


# ---------------------------------------------------------------------------
# Multi-page doctags merge
# ---------------------------------------------------------------------------


_OUTER_DOCTAG_RE = re.compile(
    r"^\s*<doctag>(?P<inner>.*?)</doctag>\s*$", re.DOTALL
)


def _strip_doctag_wrapper(doctags: str) -> str:
    """Peel the outer ``<doctag>...</doctag>`` wrapper off a per-page doctags
    string. If the wrapper is absent (some Docling versions omit the closing
    tag), return the input stripped, defensively trimming a leading
    ``<doctag>`` if present.
    """
    m = _OUTER_DOCTAG_RE.match(doctags)
    if m:
        return m.group("inner").strip("\n")
    s = doctags.strip()
    if s.startswith("<doctag>"):
        s = s[len("<doctag>") :]
    if s.endswith("</doctag>"):
        s = s[: -len("</doctag>")]
    return s.strip("\n")


def _merge_pages_with_breaks(per_page_doctags: list[str]) -> str:
    """Combine per-page doctags into a single ``<doctag>...</doctag>`` block
    with ``<page_break>`` between each page.

    The resulting string round-trips through
    ``DocTagsDocument.from_multipage_doctags_and_images`` to produce a
    DoclingDocument with one page per input.
    """
    inner_parts = [_strip_doctag_wrapper(p) for p in per_page_doctags]
    joiner = "\n<page_break>\n"
    return "<doctag>\n" + joiner.join(inner_parts) + "\n</doctag>"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _write_manifest(
    manifest_path: Path,
    *,
    settings: Any,
    started_at: datetime,
    completed_at: datetime,
    elapsed_seconds: float,
    page_count: int,
    successful_pages: list[int],
    failed_pages: list[int],
) -> None:
    payload = {
        "schema_version": 1,
        "mode": "chunked-per-page",
        "model": "ibm-granite/granite-docling-258M",
        "backend": "llama-server-gguf",
        "api_url": settings.api_url,
        "api_model": settings.api_model,
        "scale": settings.scale,
        "concurrency": settings.concurrency,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "page_count": page_count,
        "successful_pages": len(successful_pages),
        "failed_pages": failed_pages,
        "prompt": settings.prompt,
    }
    manifest_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.info("granite: manifest → %s", manifest_path)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def parse_pdf_granite(
    pdf_path: Path,
    out_path: Path,
    *,
    settings: Any,
    page_count: int,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> GraniteParseResult:
    """Run Granite-Docling-258M VLM via llama-server, save merged DoclingDocument JSON.

    Args:
        pdf_path: source PDF.
        out_path: destination ``01_raw_granite.json`` path. A sibling
            ``01_raw_granite.manifest.json`` is also written.
        settings: ``GraniteSettings`` instance (loose-typed to avoid an
            import cycle with the config module).
        page_count: total pages the caller wants converted (1..page_count).
        on_progress: optional callback ``(page_no, page_count, page_elapsed_s)``.

    Returns:
        ``GraniteParseResult`` with success/failure tallies. Per-page
        failures are captured; the only hard failure that raises is the
        initial health check (RuntimeError) and a final no-pages-succeeded
        merge failure.

    Notes:
        - Requires llama-server started with ``--special`` flag.
          See ``docs/explorations/granite-llama-server-spike.md``.
        - Single-page convert is mandatory: spike showed multi-page convert
          builds an OOM-prone batch on 8GB WSL2.
    """
    if settings.health_check:
        _health_check(settings.api_url)
    else:
        log.warning("granite: health check skipped (settings.health_check=False)")

    log.info("granite: building converter (model=%s, scale=%.1f, concurrency=%d)",
             settings.api_model, settings.scale, settings.concurrency)
    converter = _build_converter(settings)

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()

    successful_pages: list[int] = []
    failed_pages: list[int] = []
    per_page_doctags: list[str] = []
    repeated_line_warnings: list[int] = []

    for page_no in range(1, page_count + 1):
        page_t0 = time.monotonic()
        try:
            result = converter.convert(
                str(pdf_path), page_range=(page_no, page_no)
            )
            doc = result.document
            doctags = doc.export_to_doctags()

            cleaned, dropped = _detect_repeated_lines(doctags)
            if dropped:
                repeated_line_warnings.append(page_no)
                log.warning(
                    "granite: page %d had %d surplus repeated <text> lines collapsed",
                    page_no, dropped,
                )

            per_page_doctags.append(cleaned)
            successful_pages.append(page_no)

            del result, doc
            gc.collect()
        except Exception as exc:  # noqa: BLE001 — per-page isolation is the contract
            log.error("granite: [page %d/%d] ERROR: %s", page_no, page_count, exc)
            failed_pages.append(page_no)

        page_elapsed = time.monotonic() - page_t0
        if on_progress is not None:
            try:
                on_progress(page_no, page_count, page_elapsed)
            except Exception:
                log.exception("granite: on_progress callback raised; ignoring")

    elapsed_seconds = time.monotonic() - t0
    completed_at = datetime.now(tz=timezone.utc)

    if not per_page_doctags:
        raise RuntimeError(
            f"Granite parse produced 0 successful pages out of {page_count}; "
            f"failed_pages={failed_pages}"
        )

    log.info(
        "granite: merging %d page doctags with <page_break> injection",
        len(per_page_doctags),
    )
    merged = _merge_pages_with_breaks(per_page_doctags)

    # Round-trip into DoclingDocument JSON.
    from docling_core.types.doc import DocTagsDocument, DoclingDocument

    doc_tags = DocTagsDocument.from_multipage_doctags_and_images(
        doctags=merged, images=None
    )
    merged_doc = DoclingDocument.load_from_doctags(
        doc_tags, document_name=pdf_path.stem
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged_doc.save_as_json(out_path)
    log.info(
        "granite: parse → %s (pages=%d successful, %d failed, %.1fs)",
        out_path.name,
        len(successful_pages),
        len(failed_pages),
        elapsed_seconds,
    )

    manifest_path = out_path.with_name(out_path.stem + ".manifest.json")
    _write_manifest(
        manifest_path,
        settings=settings,
        started_at=started_at,
        completed_at=completed_at,
        elapsed_seconds=elapsed_seconds,
        page_count=page_count,
        successful_pages=successful_pages,
        failed_pages=failed_pages,
    )

    return GraniteParseResult(
        successful_pages=successful_pages,
        failed_pages=failed_pages,
        elapsed_seconds=elapsed_seconds,
        out_path=out_path,
        page_count=page_count,
        manifest_path=manifest_path,
        repeated_line_warnings=repeated_line_warnings,
    )
