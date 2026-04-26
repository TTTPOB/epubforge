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


@dataclass
class GranitePagesResult:
    """Intermediate result of the per-page Granite loop.

    Used internally by ``parse_pdf_granite`` and by the segmented-parser
    subprocess worker so it can serialise per-page doctags to disk and
    have the parent process do the final merge.
    """

    per_page_doctags: list[str]
    successful_pages: list[int]
    failed_pages: list[int]
    repeated_line_warnings: list[int]
    elapsed_seconds: float


def _granite_pages_to_doctags(
    pdf_path: Path,
    *,
    settings: Any,
    page_range: tuple[int, int],
    on_progress: Callable[[int, int, float], None] | None = None,
) -> GranitePagesResult:
    """Per-page Granite loop over ``[start, end]`` (1-based, inclusive).

    Returns the per-page doctags strings (in absolute page order) and the
    list of absolute page numbers that succeeded / failed. The caller is
    responsible for merging the doctags into a DoclingDocument and writing
    the on-disk artefacts.

    This factoring exists so the segmented parser can run this loop inside
    a short-lived subprocess (to bound onnxruntime/torch mmap accumulation)
    while the parent process keeps responsibility for the final merge.
    """
    start, end = page_range
    if start < 1 or end < start:
        raise ValueError(f"Invalid page_range: {page_range!r}")

    t0 = time.monotonic()
    successful_pages: list[int] = []
    failed_pages: list[int] = []
    per_page_doctags: list[str] = []
    repeated_line_warnings: list[int] = []

    total = end - start + 1
    for page_no in range(start, end + 1):
        page_t0 = time.monotonic()
        # Build a fresh converter per page so docling-internal caches do not
        # accumulate across pages. Combined with del + gc.collect() below
        # this keeps RSS bounded on 8GB WSL2 (review-agent finding).
        converter = _build_converter(settings)
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
        except Exception as exc:  # noqa: BLE001 — per-page isolation is the contract
            log.error("granite: [page %d/%d] ERROR: %s", page_no, end, exc)
            failed_pages.append(page_no)
        finally:
            del converter
            gc.collect()

        page_elapsed = time.monotonic() - page_t0
        if on_progress is not None:
            try:
                on_progress(page_no, total, page_elapsed)
            except Exception:
                log.exception("granite: on_progress callback raised; ignoring")

    return GranitePagesResult(
        per_page_doctags=per_page_doctags,
        successful_pages=successful_pages,
        failed_pages=failed_pages,
        repeated_line_warnings=repeated_line_warnings,
        elapsed_seconds=time.monotonic() - t0,
    )


def _finalize_granite_document(
    per_page_doctags: list[str],
    *,
    pdf_path: Path,
    out_path: Path,
) -> None:
    """Merge per-page doctags into a DoclingDocument JSON at ``out_path``."""
    if not per_page_doctags:
        raise RuntimeError("granite: refusing to write empty document")

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


def parse_pdf_granite(
    pdf_path: Path,
    out_path: Path,
    *,
    settings: Any,
    page_count: int,
    page_range: tuple[int, int] | None = None,
    on_progress: Callable[[int, int, float], None] | None = None,
    write_manifest: bool = True,
) -> GraniteParseResult:
    """Run Granite-Docling-258M VLM via llama-server, save merged DoclingDocument JSON.

    Args:
        pdf_path: source PDF.
        out_path: destination ``01_raw_granite.json`` path. A sibling
            ``01_raw_granite.manifest.json`` is also written when
            ``write_manifest=True``.
        settings: ``GraniteSettings`` instance (loose-typed to avoid an
            import cycle with the config module).
        page_count: total pages of the source PDF (used for manifest book-keeping).
        page_range: optional 1-based inclusive ``(start, end)`` page range to
            convert. When None, the full ``[1, page_count]`` range is used.
            ``page_count`` is recorded in the manifest regardless of the range.
        on_progress: optional callback ``(page_no, total, page_elapsed_s)``.
        write_manifest: when False, the sidecar manifest is not written.
            Used by the segmented parser to suppress per-segment manifests
            (the parent process writes a single summary manifest).

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

    log.info(
        "granite: per-page converter mode (model=%s, scale=%.1f, concurrency=%d)",
        settings.api_model, settings.scale, settings.concurrency,
    )

    if page_range is None:
        page_range = (1, page_count)

    started_at = datetime.now(tz=timezone.utc)
    pages_result = _granite_pages_to_doctags(
        pdf_path,
        settings=settings,
        page_range=page_range,
        on_progress=on_progress,
    )
    completed_at = datetime.now(tz=timezone.utc)

    if not pages_result.per_page_doctags:
        raise RuntimeError(
            f"Granite parse produced 0 successful pages out of "
            f"{page_range[1] - page_range[0] + 1}; "
            f"failed_pages={pages_result.failed_pages}"
        )

    _finalize_granite_document(
        pages_result.per_page_doctags,
        pdf_path=pdf_path,
        out_path=out_path,
    )
    log.info(
        "granite: parse → %s (pages=%d successful, %d failed, %.1fs)",
        out_path.name,
        len(pages_result.successful_pages),
        len(pages_result.failed_pages),
        pages_result.elapsed_seconds,
    )

    manifest_path: Path | None = None
    if write_manifest:
        manifest_path = out_path.with_name(out_path.stem + ".manifest.json")
        _write_manifest(
            manifest_path,
            settings=settings,
            started_at=started_at,
            completed_at=completed_at,
            elapsed_seconds=pages_result.elapsed_seconds,
            page_count=page_count,
            successful_pages=pages_result.successful_pages,
            failed_pages=pages_result.failed_pages,
        )

    return GraniteParseResult(
        successful_pages=pages_result.successful_pages,
        failed_pages=pages_result.failed_pages,
        elapsed_seconds=pages_result.elapsed_seconds,
        out_path=out_path,
        page_count=page_count,
        manifest_path=manifest_path,
        repeated_line_warnings=pages_result.repeated_line_warnings,
    )


def parse_pdf_granite_segmented(
    pdf_path: Path,
    out_path: Path,
    *,
    settings: Any,
    page_count: int,
    segment_size: int,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> GraniteParseResult:
    """Run the Granite per-page loop in subprocess-isolated segments.

    Each segment is processed by ``epubforge.parser._segment_worker granite``
    which invokes ``_granite_pages_to_doctags`` on ``[seg_start, seg_end]``
    and writes a JSON envelope with per-page doctags. After every segment
    subprocess exits the OS reclaims any mmap pages it held, bounding peak
    RSS at one segment's worth.

    The parent process aggregates all segments' per-page doctags (in
    absolute page order) and performs a single final
    ``_finalize_granite_document`` + manifest write — the on-disk
    ``01_raw_granite.json`` is byte-equivalent to a single-process run.

    On segment failure: subprocess produced no JSON ⇒ raise; intermediate
    segment files are preserved under ``<out_path>.segments/``.
    """
    import subprocess
    import sys

    if segment_size <= 0:
        raise ValueError(f"segment_size must be > 0, got {segment_size}")

    # Health-check is the only fail-fast moment. We do it once in the parent
    # so we don't pay rasterisation cost per-segment if llama-server is down.
    if settings.health_check:
        _health_check(settings.api_url)
    else:
        log.warning("granite: health check skipped (settings.health_check=False)")

    log.info(
        "granite: pdf=%s page_count=%d segment_size=%d segments=%d (subprocess-isolated)",
        pdf_path.name,
        page_count,
        segment_size,
        (page_count + segment_size - 1) // segment_size,
    )

    settings_json = settings.model_dump_json()

    segments_dir = out_path.with_suffix(out_path.suffix + ".segments")
    segments_dir.mkdir(parents=True, exist_ok=True)

    # Suppress per-segment health-checks (parent already did it). We mutate
    # a copy of the JSON payload only — the original ``settings`` is untouched.
    settings_dict = json.loads(settings_json)
    settings_dict["health_check"] = False
    worker_settings_json = json.dumps(settings_dict, ensure_ascii=False)

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()

    all_doctags: list[str] = []
    all_succ: list[int] = []
    all_fail: list[int] = []
    all_repeated: list[int] = []
    seg_outs: list[Path] = []

    seg_idx = 0
    for seg_start in range(1, page_count + 1, segment_size):
        seg_end = min(seg_start + segment_size - 1, page_count)
        seg_out = segments_dir / f"granite_segment_{seg_idx:03d}.json"
        seg_outs.append(seg_out)
        log.info(
            "granite: segment %d pages=[%d..%d] → subprocess",
            seg_idx + 1,
            seg_start,
            seg_end,
        )

        cmd = [
            sys.executable,
            "-m",
            "epubforge.parser._segment_worker",
            "granite",
            "--pdf",
            str(pdf_path),
            "--out",
            str(seg_out),
            "--start",
            str(seg_start),
            "--end",
            str(seg_end),
            "--granite-json",
            worker_settings_json,
        ]

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"granite: segment {seg_idx + 1} pages=[{seg_start}..{seg_end}] "
                f"failed with exit code {exc.returncode}; intermediate segment "
                f"artefacts preserved under {segments_dir}"
            ) from exc

        seg_payload = json.loads(seg_out.read_text(encoding="utf-8"))
        all_doctags.extend(seg_payload.get("per_page_doctags") or [])
        all_succ.extend(seg_payload.get("successful_pages") or [])
        all_fail.extend(seg_payload.get("failed_pages") or [])
        all_repeated.extend(seg_payload.get("repeated_line_warnings") or [])

        if on_progress is not None:
            try:
                # Per-segment progress only — page-level progress lives in worker.
                on_progress(
                    seg_end,
                    page_count,
                    float(seg_payload.get("elapsed_seconds") or 0.0),
                )
            except Exception:
                log.exception("granite: on_progress callback raised; ignoring")

        seg_idx += 1

    elapsed_seconds = time.monotonic() - t0
    completed_at = datetime.now(tz=timezone.utc)

    if not all_doctags:
        raise RuntimeError(
            f"Granite segmented parse produced 0 successful pages out of "
            f"{page_count}; failed_pages={all_fail}"
        )

    _finalize_granite_document(
        all_doctags, pdf_path=pdf_path, out_path=out_path
    )
    log.info(
        "granite: segmented parse → %s (pages=%d successful, %d failed, %.1fs)",
        out_path.name,
        len(all_succ),
        len(all_fail),
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
        successful_pages=all_succ,
        failed_pages=all_fail,
    )

    # Cleanup segment files on success only.
    for seg_out in seg_outs:
        try:
            seg_out.unlink()
        except OSError as exc:
            log.warning("granite: failed to remove segment file %s: %s", seg_out, exc)
    try:
        segments_dir.rmdir()
    except OSError:
        pass

    return GraniteParseResult(
        successful_pages=all_succ,
        failed_pages=all_fail,
        elapsed_seconds=elapsed_seconds,
        out_path=out_path,
        page_count=page_count,
        manifest_path=manifest_path,
        repeated_line_warnings=all_repeated,
    )
