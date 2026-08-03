"""Tests for the agent-facing Paddle page-image CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import fitz
import pytest

from epubforge import paddle_page_cli
from epubforge.paddle_common import PaddleError


LAYOUT_RAW = {
    "res": {
        "boxes": [
            {"label": "image", "score": 0.95, "coordinate": [10, 10, 90, 60]},
            {"label": "text", "score": 0.9, "coordinate": [10, 10, 90, 60]},
        ]
    }
}
TEXT_RAW = {
    "res": {
        "dt_polys": [[[10, 10], [90, 10], [90, 30], [10, 30]]],
        "dt_scores": [0.8],
    }
}


class _FakeModel:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.predictions: list[str] = []

    def predict(self, source: str, *, batch_size: int) -> Any:
        assert batch_size == 1
        self.predictions.append(source)
        return self.raw


def _write_jpeg(
    path: Path,
    width: int = 100,
    height: int = 120,
    *,
    fill: tuple[float, float, float] = (1, 1, 1),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, fill=fill)
    path.write_bytes(page.get_pixmap(alpha=False).tobytes("jpeg", jpg_quality=82))
    document.close()


def _fake_models(
    monkeypatch: pytest.MonkeyPatch,
    layout_raw: Any = LAYOUT_RAW,
    text_raw: Any = TEXT_RAW,
) -> tuple[_FakeModel, _FakeModel, list[tuple[Any, Any]]]:
    layout_model = _FakeModel(layout_raw)
    text_model = _FakeModel(text_raw)
    constructed: list[tuple[Any, Any]] = []

    def fake_load_models(
        _args: Any, _source: Path, _page_number: int
    ) -> tuple[Any, Any]:
        constructed.append((layout_model, text_model))
        return layout_model, text_model

    monkeypatch.setattr(paddle_page_cli, "load_models", fake_load_models)
    return layout_model, text_model, constructed


def _image_pair(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "pages/page-0001.jpg"
    second = tmp_path / "pages/page-0002.jpg"
    first.parent.mkdir(parents=True)
    _write_jpeg(first)
    _write_jpeg(second, fill=(0.9, 0.8, 0.7))
    return first, second


def test_import_does_not_import_paddleocr() -> None:
    assert "paddleocr" not in sys.modules


def _parse(*extra: str, image: Path, page_idx: int, output: Path) -> Any:
    return paddle_page_cli.build_parser().parse_args(
        [
            "--image",
            str(image),
            "--page-idx",
            str(page_idx),
            "--output",
            str(output),
            *extra,
        ]
    )


def test_execute_batch_pages_shares_models_and_writes_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    evidence_out = tmp_path / "paddle-evidence.json"
    layout_model, text_model, constructed = _fake_models(monkeypatch)

    args = paddle_page_cli.build_parser().parse_args(
        [
            "--image",
            str(first),
            "--page-idx",
            "1",
            "--image",
            str(second),
            "--page-idx",
            "2",
            "--output",
            str(output),
            "--evidence-out",
            str(evidence_out),
        ]
    )
    run = paddle_page_cli.execute(args)

    assert len(constructed) == 1
    assert len(run["pages"]) == 2
    assert [page["page_idx"] for page in run["pages"]] == [1, 2]
    assert [page["reused_raw"] for page in run["pages"]] == [False, False]
    for page_idx in (1, 2):
        page_dir = output / f"page-{page_idx:04d}"
        assert (page_dir / "source.jpg").is_file()
        assert (page_dir / "layout_raw.json").is_file()
        assert (page_dir / "text_raw.json").is_file()
        assert (page_dir / "raw_meta.json").is_file()
        assert (page_dir / "evidence.json").is_file()
        assert (page_dir / "candidate.json").is_file()
        assert (page_dir / "layout_raw.jpg").read_bytes().startswith(b"\xff\xd8")
        assert (page_dir / "text_raw.jpg").read_bytes().startswith(b"\xff\xd8")
        assert (page_dir / "annotated.jpg").read_bytes().startswith(b"\xff\xd8")

    merged = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert merged["schema_version"] == 1
    assert [page["page_idx"] for page in merged["pages"]] == [1, 2]
    page_one = merged["pages"][0]
    assert page_one["source_image"] == str(first)
    assert page_one["image"] == {"width": 100, "height": 120}
    assert page_one["evidence"]["page"] == 1
    assert page_one["candidate"]["page"] == 1
    candidate_ids = [box["id"] for box in page_one["candidate"]["boxes"]]
    assert all(candidate_id.startswith("p0001-") for candidate_id in candidate_ids)


def test_execute_reuse_raw_skips_models_and_reruns_postprocessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    evidence_out = tmp_path / "paddle-evidence.json"
    _fake_models(monkeypatch)

    first_args = _parse(
        "--evidence-out", str(evidence_out), image=first, page_idx=1, output=output
    )
    paddle_page_cli.execute(first_args)

    def fail_load_models(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("reuse path must not construct Paddle models")

    monkeypatch.setattr(paddle_page_cli, "load_models", fail_load_models)
    reuse_args = _parse(
        "--reuse-raw",
        "--vertical-gap",
        "0.05",
        "--evidence-out",
        str(evidence_out),
        image=first,
        page_idx=1,
        output=output,
    )
    run = paddle_page_cli.execute(reuse_args)

    assert run["pages"][0]["reused_raw"] is True
    candidate = json.loads(
        (output / "page-0001" / "candidate.json").read_text(encoding="utf-8")
    )
    assert candidate["postprocess_params"]["vertical_gap"] == 0.05
    merged = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert len(merged["pages"]) == 1
    assert merged["pages"][0]["candidate"]["postprocess_params"]["vertical_gap"] == 0.05


def test_execute_reuse_rejects_parameter_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    _fake_models(monkeypatch)

    paddle_page_cli.execute(_parse(image=first, page_idx=1, output=output))

    mismatched = _parse(
        "--reuse-raw",
        "--layout-threshold",
        "0.8",
        image=first,
        page_idx=1,
        output=output,
    )
    with pytest.raises(PaddleError, match="raw parameters do not match"):
        paddle_page_cli.execute(mismatched)


def test_execute_reuse_rejects_different_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    _fake_models(monkeypatch)

    paddle_page_cli.execute(_parse(image=first, page_idx=1, output=output))

    different = _parse(
        "--reuse-raw",
        image=second,
        page_idx=1,
        output=output,
    )
    with pytest.raises(PaddleError, match="raw parameters do not match"):
        paddle_page_cli.execute(different)


def test_reuse_rejects_cached_image_identity_mismatch(tmp_path: Path) -> None:
    """A cache whose params match but image hash differs must be refused."""
    first, second = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    page_dir = output / "page-0001"
    page_dir.mkdir(parents=True)
    (page_dir / "source.jpg").write_bytes(first.read_bytes())
    (page_dir / "layout_raw.json").write_text(json.dumps(LAYOUT_RAW), encoding="utf-8")
    (page_dir / "text_raw.json").write_text(json.dumps(TEXT_RAW), encoding="utf-8")
    args = _parse(image=first, page_idx=1, output=output)
    inference_params = paddle_page_cli._inference_params(
        args, paddle_page_cli._file_sha256(first)
    )
    paddle_page_cli._write_json(
        page_dir / "raw_meta.json",
        {
            "schema_version": paddle_page_cli.RAW_SCHEMA_VERSION,
            "page": 1,
            "image_sha256": paddle_page_cli._file_sha256(second),
            "image": {"width": 100, "height": 120},
            "inference_params": inference_params,
            "versions": {"paddleocr": "test", "paddlepaddle": "test"},
            "files": {
                name: {"sha256": paddle_page_cli._file_sha256(page_dir / name)}
                for name in ("source.jpg", "layout_raw.json", "text_raw.json")
            },
        },
    )

    with pytest.raises(PaddleError, match="different image"):
        paddle_page_cli.execute(
            _parse("--reuse-raw", image=first, page_idx=1, output=output)
        )


def test_execute_applies_page_patch_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    patch = tmp_path / "page-1-patch.py"
    patch.write_text(
        """def patch_page(context):
    assert context["page"] == 1
    assert context["image"] == {"width": 100, "height": 120}
    body = next(box for box in context["candidate_boxes"] if box["type"] == "BODY")
    body["type"] = "TITLE"
    return [body]
""",
        encoding="utf-8",
    )
    _fake_models(monkeypatch)

    args = paddle_page_cli.build_parser().parse_args(
        [
            "--image",
            str(first),
            "--page-idx",
            "1",
            "--output",
            str(output),
            "--page-patch",
            str(patch),
        ]
    )
    run = paddle_page_cli.execute(args)

    candidate = json.loads(
        (output / "page-0001" / "candidate.json").read_text(encoding="utf-8")
    )
    assert len(candidate["boxes"]) == 1
    patched = candidate["boxes"][0]
    assert patched["type"] == "TITLE"
    assert patched["source"] == "page_patch"
    expected_record = {
        "path": str(patch.resolve()),
        "sha256": paddle_page_cli._file_sha256(patch),
    }
    assert candidate["page_patch"] == expected_record
    assert run["pages"][0]["page_patch"] == expected_record


def test_evidence_out_merges_replace_and_keeps_pages_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    evidence_out = tmp_path / "paddle-evidence.json"
    _fake_models(monkeypatch)

    paddle_page_cli.execute(
        _parse(
            "--evidence-out", str(evidence_out), image=first, page_idx=1, output=output
        )
    )
    paddle_page_cli.execute(
        _parse(
            "--evidence-out", str(evidence_out), image=second, page_idx=2, output=output
        )
    )
    merged = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert [page["page_idx"] for page in merged["pages"]] == [1, 2]

    # Rerunning page 1 replaces its entry without duplicating it.
    paddle_page_cli.execute(
        _parse(
            "--evidence-out", str(evidence_out), image=first, page_idx=1, output=output
        )
    )
    merged = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert [page["page_idx"] for page in merged["pages"]] == [1, 2]


def test_execute_rejects_non_jpeg_and_pdf_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "paddle"
    _fake_models(monkeypatch)
    not_image = tmp_path / "pages/page-0001.txt"
    not_image.parent.mkdir(parents=True)
    not_image.write_text("not a jpeg", encoding="utf-8")
    with pytest.raises(PaddleError, match="JPEG"):
        paddle_page_cli.execute(_parse(image=not_image, page_idx=1, output=output))

    pdf = tmp_path / "pages/page-0001.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PaddleError, match="must not be a PDF"):
        paddle_page_cli.execute(_parse(image=pdf, page_idx=1, output=output))

    jpeg_named_pdf = tmp_path / "pages/page-0001.jpg"
    jpeg_named_pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PaddleError, match="not a JPEG"):
        paddle_page_cli.execute(_parse(image=jpeg_named_pdf, page_idx=1, output=output))


@pytest.mark.parametrize(
    ("images", "indices", "message"),
    [
        (["a.jpg"], [], "same number"),
        ([], [1], "same number"),
        (["a.jpg", "b.jpg"], [1], "same number"),
        (["a.jpg", "a.jpg"], [1, 1], "duplicate"),
        (["a.jpg"], [-1], "non-negative"),
    ],
)
def test_page_pair_validation(
    tmp_path: Path, images: list[str], indices: list[int], message: str
) -> None:
    paths = [tmp_path / name for name in images]
    with pytest.raises(PaddleError, match=message):
        paddle_page_cli._parse_page_pairs(paths, indices)


def test_validate_args_rejects_nonfinite_parameters(
    tmp_path: Path,
) -> None:
    image = tmp_path / "page-0001.jpg"
    _write_jpeg(image)
    parser = paddle_page_cli.build_parser()
    for option in ("--caption-gap", "--layout-threshold", "--db-unclip-ratio"):
        args = parser.parse_args(
            [
                "--image",
                str(image),
                "--page-idx",
                "1",
                "--output",
                str(tmp_path / "out"),
                option,
                "nan",
            ]
        )
        with pytest.raises(SystemExit):
            paddle_page_cli._validate_args(parser, args)


def test_cli_requires_image_and_page_idx_pairs(
    tmp_path: Path,
) -> None:
    parser = paddle_page_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--image", str(tmp_path / "a.jpg"), "--output", str(tmp_path)]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["--page-idx", "1", "--output", str(tmp_path)])


def test_evidence_merge_records_source_image_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    evidence_out = tmp_path / "paddle-evidence.json"
    _fake_models(monkeypatch)

    paddle_page_cli.execute(
        _parse(
            "--evidence-out", str(evidence_out), image=first, page_idx=1, output=output
        )
    )
    merged = json.loads(evidence_out.read_text(encoding="utf-8"))
    assert merged["pages"][0]["source_image_sha256"] == paddle_page_cli._file_sha256(
        first
    )


def test_execute_rejects_too_many_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = []
    for index in range(3):
        image = tmp_path / f"pages/page-{index:04d}.jpg"
        _write_jpeg(image)
        images.append(str(image))
    monkeypatch.setattr(paddle_page_cli, "MAX_PAGES_PER_RUN", 2)

    def fail_load_models(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("limits must fire before model construction")

    monkeypatch.setattr(paddle_page_cli, "load_models", fail_load_models)
    args = paddle_page_cli.build_parser().parse_args(
        [
            "--image",
            images[0],
            "--page-idx",
            "0",
            "--image",
            images[1],
            "--page-idx",
            "1",
            "--image",
            images[2],
            "--page-idx",
            "2",
            "--output",
            str(tmp_path / "out"),
        ]
    )
    with pytest.raises(PaddleError, match="at most 2 pages"):
        paddle_page_cli.execute(args)


def test_execute_rejects_oversized_page_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image, width=100, height=120)
    monkeypatch.setattr(paddle_page_cli, "MAX_PAGE_PIXELS", 100 * 120 - 1)

    def fail_load_models(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pixel limit must fire before model construction")

    monkeypatch.setattr(paddle_page_cli, "load_models", fail_load_models)
    with pytest.raises(PaddleError, match="pixel limit"):
        paddle_page_cli.execute(
            _parse(image=image, page_idx=1, output=tmp_path / "out")
        )


def test_execute_enforces_total_output_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)

    def fail_load_models(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("budget must fire before model construction")

    monkeypatch.setattr(paddle_page_cli, "load_models", fail_load_models)
    monkeypatch.setattr(paddle_page_cli, "MAX_TOTAL_OUTPUT_BYTES", 1)
    with pytest.raises(PaddleError, match="total byte limit"):
        paddle_page_cli.execute(
            _parse(image=image, page_idx=1, output=tmp_path / "out")
        )


def test_evidence_out_aggregate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)
    _fake_models(monkeypatch)
    monkeypatch.setattr(paddle_page_cli, "MAX_EVIDENCE_OUT_BYTES", 10)

    with pytest.raises(PaddleError, match="exceeds the size limit"):
        paddle_page_cli.execute(
            _parse(
                "--evidence-out",
                str(tmp_path / "paddle-evidence.json"),
                image=image,
                page_idx=1,
                output=tmp_path / "out",
            )
        )


def test_reuse_rejects_cached_source_jpg_mismatch(tmp_path: Path) -> None:
    """source.jpg, input image, and metadata must all be one snapshot."""
    first, second = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    page_dir = output / "page-0001"
    page_dir.mkdir(parents=True)
    # Cache whose metadata matches the *input* image but whose source.jpg
    # copy holds different bytes with internally consistent file hashes.
    (page_dir / "source.jpg").write_bytes(second.read_bytes())
    (page_dir / "layout_raw.json").write_text(json.dumps(LAYOUT_RAW), encoding="utf-8")
    (page_dir / "text_raw.json").write_text(json.dumps(TEXT_RAW), encoding="utf-8")
    input_sha = paddle_page_cli._file_sha256(first)
    args = _parse(image=first, page_idx=1, output=output)
    inference_params = paddle_page_cli._inference_params(args, input_sha)
    paddle_page_cli._write_json(
        page_dir / "raw_meta.json",
        {
            "schema_version": paddle_page_cli.RAW_SCHEMA_VERSION,
            "page": 1,
            "image_sha256": input_sha,
            "image": {"width": 100, "height": 120},
            "inference_params": inference_params,
            "versions": {"paddleocr": "test", "paddlepaddle": "test"},
            "files": {
                name: {"sha256": paddle_page_cli._file_sha256(page_dir / name)}
                for name in ("source.jpg", "layout_raw.json", "text_raw.json")
            },
        },
    )

    with pytest.raises(PaddleError, match="cached source.jpg does not match"):
        paddle_page_cli.execute(
            _parse("--reuse-raw", image=first, page_idx=1, output=output)
        )


def _oversized_jpeg(path: Path, width: int = 5001, height: int = 5001) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sof = (
        b"\xff\xc0"
        + b"\x00\x0b"
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01"
        + b"\x01\x11\x00"
    )
    path.write_bytes(b"\xff\xd8" + sof + b"\xff\xd9")


def test_execute_rejects_oversized_jpeg_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _oversized_jpeg(image)

    def fail_decode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("oversized input must not reach Pixmap decode")

    monkeypatch.setattr(fitz, "Pixmap", fail_decode)
    monkeypatch.setattr(
        paddle_page_cli, "load_models", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("oversized input must not reach model loading")
        )
    )
    with pytest.raises(PaddleError, match="pixel limit"):
        paddle_page_cli.execute(_parse(image=image, page_idx=1, output=tmp_path / "out"))


def test_reuse_rejects_oversized_cached_source_jpeg_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _image_pair(tmp_path)
    output = tmp_path / "paddle"
    page_dir = output / "page-0001"
    page_dir.mkdir(parents=True)
    _oversized_jpeg(page_dir / "source.jpg")
    (page_dir / "layout_raw.json").write_text(json.dumps(LAYOUT_RAW), encoding="utf-8")
    (page_dir / "text_raw.json").write_text(json.dumps(TEXT_RAW), encoding="utf-8")
    input_sha = paddle_page_cli._file_sha256(first)
    args = _parse(image=first, page_idx=1, output=output)
    inference_params = paddle_page_cli._inference_params(args, input_sha)
    paddle_page_cli._write_json(
        page_dir / "raw_meta.json",
        {
            "schema_version": paddle_page_cli.RAW_SCHEMA_VERSION,
            "page": 1,
            "image_sha256": input_sha,
            "image": {"width": 100, "height": 120},
            "inference_params": inference_params,
            "versions": {"paddleocr": "test", "paddlepaddle": "test"},
            "files": {
                name: {"sha256": paddle_page_cli._file_sha256(page_dir / name)}
                for name in ("source.jpg", "layout_raw.json", "text_raw.json")
            },
        },
    )

    def fail_decode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("oversized cache must not reach Pixmap decode")

    monkeypatch.setattr(fitz, "Pixmap", fail_decode)
    with pytest.raises(PaddleError, match="cached source JPEG exceeds the pixel limit"):
        paddle_page_cli.execute(
            _parse("--reuse-raw", image=first, page_idx=1, output=output)
        )


def test_evidence_out_counts_toward_total_output_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)
    _fake_models(monkeypatch)
    output = tmp_path / "paddle"
    evidence_out = tmp_path / "paddle-evidence.json"

    # First run without evidence-out establishes the artifact total.
    paddle_page_cli.execute(_parse(image=image, page_idx=1, output=output))
    total_artifacts = sum(
        path.stat().st_size for path in output.rglob("*") if path.is_file()
    )
    monkeypatch.setattr(
        paddle_page_cli, "MAX_TOTAL_OUTPUT_BYTES", total_artifacts
    )

    with pytest.raises(PaddleError, match="total byte limit"):
        paddle_page_cli.execute(
            _parse(
                "--evidence-out", str(evidence_out), image=image, page_idx=1, output=output
            )
        )


def test_evidence_merge_charges_budget_per_write(tmp_path: Path) -> None:
    charges: list[int] = []

    def budget(size: int) -> None:
        charges.append(size)
        if len(charges) > 1:
            raise PaddleError("paddle output exceeds the total byte limit")

    evidence_out = tmp_path / "paddle-evidence.json"
    paddle_page_cli._page_evidence_merge(
        evidence_out,
        1,
        source_image="pages/page-0001.jpg",
        source_image_sha256="a" * 64,
        image={"width": 100, "height": 120},
        evidence={"page": 1},
        candidate={"page": 1, "boxes": []},
        budget=budget,
    )
    assert len(charges) == 1
    with pytest.raises(PaddleError, match="total byte limit"):
        paddle_page_cli._page_evidence_merge(
            evidence_out,
            2,
            source_image="pages/page-0002.jpg",
            source_image_sha256="b" * 64,
            image={"width": 100, "height": 120},
            evidence={"page": 2},
            candidate={"page": 2, "boxes": []},
            budget=budget,
        )
    assert len(charges) == 2


def test_evidence_out_existing_oversized_file_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)
    _fake_models(monkeypatch)
    monkeypatch.setattr(paddle_page_cli, "MAX_EVIDENCE_OUT_BYTES", 50)
    evidence_out = tmp_path / "paddle-evidence.json"
    evidence_out.write_bytes(b"x" * 200)  # garbage larger than the cap

    with pytest.raises(PaddleError, match="aggregated paddle evidence exceeds"):
        paddle_page_cli.execute(
            _parse(
                "--evidence-out", str(evidence_out), image=image, page_idx=1, output=tmp_path / "out"
            )
        )


@pytest.mark.parametrize("root", [None, [], "text", 42])
def test_validate_reuse_meta_rejects_non_mapping_root(
    tmp_path: Path, root: Any
) -> None:
    page_dir = tmp_path / "page-0001"
    page_dir.mkdir()
    (page_dir / "raw_meta.json").write_text(json.dumps(root), encoding="utf-8")

    with pytest.raises(PaddleError, match="must be a JSON object"):
        paddle_page_cli._validate_reuse_meta(page_dir / "raw_meta.json", {}, 1)


@pytest.mark.parametrize("root", [None, [], "text", 42])
def test_evidence_merge_rejects_non_mapping_existing_file(
    tmp_path: Path, root: Any
) -> None:
    evidence_out = tmp_path / "paddle-evidence.json"
    evidence_out.write_text(json.dumps(root), encoding="utf-8")

    with pytest.raises(PaddleError, match="invalid schema"):
        paddle_page_cli._page_evidence_merge(
            evidence_out,
            1,
            source_image="pages/page-0001.jpg",
            source_image_sha256="a" * 64,
            image={"width": 100, "height": 120},
            evidence={"page": 1},
            candidate={"page": 1, "boxes": []},
        )


def test_input_image_is_snapshotted_once(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)
    good = image.read_bytes()
    evil = b"\xff\xd8" + b"tampered after first read"
    _fake_models(monkeypatch)

    class FlipFlop:
        def __init__(self) -> None:
            self.count = 0

        def __enter__(self) -> "FlipFlop":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self, n: int = -1) -> bytes:
            self.count += 1
            return good if self.count == 1 else evil

    flip = FlipFlop()
    real_open = Path.open

    def tracked_open(path, *args, **kwargs):
        if Path(path) == image:
            return flip
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    output = tmp_path / "paddle"
    paddle_page_cli.execute(_parse(image=image, page_idx=1, output=output))

    assert flip.count == 1
    assert (output / "page-0001" / "source.jpg").read_bytes() == good
    merged_meta = json.loads(
        (output / "page-0001" / "raw_meta.json").read_text(encoding="utf-8")
    )
    assert merged_meta["image_sha256"] == paddle_page_cli.hashlib.sha256(good).hexdigest()


def test_reuse_path_reads_input_snapshot_once(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "pages/page-0001.jpg"
    _write_jpeg(image)
    good = image.read_bytes()
    output = tmp_path / "paddle"
    _fake_models(monkeypatch)
    paddle_page_cli.execute(_parse(image=image, page_idx=1, output=output))

    evil = b"\xff\xd8" + b"tampered after first read"

    class FlipFlop:
        def __init__(self) -> None:
            self.count = 0

        def __enter__(self) -> "FlipFlop":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self, n: int = -1) -> bytes:
            self.count += 1
            return good if self.count == 1 else evil

    flip = FlipFlop()
    real_open = Path.open

    def tracked_open(path, *args, **kwargs):
        if Path(path) == image:
            return flip
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    run = paddle_page_cli.execute(
        _parse("--reuse-raw", image=image, page_idx=1, output=output)
    )
    assert flip.count == 1
    assert run["pages"][0]["reused_raw"] is True


def _valid_evidence_page(page_idx: int = 1) -> dict[str, Any]:
    return {
        "page_idx": page_idx,
        "source_image": f"pages/page-{page_idx:04d}.jpg",
        "source_image_sha256": f"{page_idx:02d}" * 32,
        "image": {"width": 100, "height": 120},
        "evidence": {"page": page_idx},
        "candidate": {"page": page_idx, "boxes": []},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda page: page.pop("source_image_sha256"),
        lambda page: page.update({"extra": 1}),
        lambda page: page.__setitem__("page_idx", True),
        lambda page: page.__setitem__("page_idx", "1"),
        lambda page: page.__setitem__("page_idx", -1),
        lambda page: page["image"].__setitem__("width", True),
        lambda page: page["image"].__setitem__("width", "100"),
        lambda page: page["image"].__setitem__("height", 0),
        lambda page: page.__setitem__("evidence", []),
        lambda page: page.__setitem__("candidate", "x"),
        lambda page: page.__setitem__("source_image", ""),
        lambda page: page.__setitem__("source_image_sha256", "zz"),
        lambda page: page.__setitem__("image", None),
    ],
)
def test_evidence_merge_rejects_invalid_existing_pages(
    tmp_path: Path, mutate
) -> None:
    evidence_out = tmp_path / "paddle-evidence.json"
    page = _valid_evidence_page()
    mutate(page)
    evidence_out.write_text(
        json.dumps({"schema_version": 1, "pages": [page]}), encoding="utf-8"
    )

    with pytest.raises(PaddleError):
        paddle_page_cli._page_evidence_merge(
            evidence_out,
            2,
            source_image="pages/page-0002.jpg",
            source_image_sha256="b" * 64,
            image={"width": 100, "height": 120},
            evidence={"page": 2},
            candidate={"page": 2, "boxes": []},
        )


def test_evidence_merge_rejects_duplicate_existing_page_idx(tmp_path: Path) -> None:
    evidence_out = tmp_path / "paddle-evidence.json"
    evidence_out.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pages": [_valid_evidence_page(1), _valid_evidence_page(1)],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PaddleError, match="repeats page_idx 1"):
        paddle_page_cli._page_evidence_merge(
            evidence_out,
            2,
            source_image="pages/page-0002.jpg",
            source_image_sha256="b" * 64,
            image={"width": 100, "height": 120},
            evidence={"page": 2},
            candidate={"page": 2, "boxes": []},
        )
