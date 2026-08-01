from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import fitz
import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "paddle_layout_tune.py"
SPEC = importlib.util.spec_from_file_location("paddle_layout_tune", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
PADDLEOCR_WAS_IMPORTED = "paddleocr" in sys.modules
tune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tune)
PADDLEOCR_IMPORTED_BY_SCRIPT = not PADDLEOCR_WAS_IMPORTED and "paddleocr" in sys.modules


def _write_jpeg(path: Path, width: int = 100, height: int = 120) -> None:
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, fill=(1, 1, 1))
    path.write_bytes(page.get_pixmap(alpha=False).tobytes("jpeg", jpg_quality=82))
    document.close()


def _write_reuse_artifacts(
    page_dir: Path,
    inference_params: Mapping[str, object],
    *,
    page_number: int = 1,
    width: int = 100,
    height: int = 120,
) -> Path:
    page_dir.mkdir(parents=True, exist_ok=True)
    _write_jpeg(page_dir / "source.jpg", width, height)
    tune._write_json(
        page_dir / "layout_raw.json",
        {
            "res": {
                "boxes": [
                    {"label": "text", "score": 0.9, "coordinate": [10, 10, 90, 40]}
                ]
            }
        },
    )
    tune._write_json(
        page_dir / "text_raw.json",
        {
            "res": {
                "dt_polys": [[[10, 10], [90, 10], [90, 20], [10, 20]]],
                "dt_scores": [0.8],
            }
        },
    )
    meta_path = page_dir / "raw_meta.json"
    tune._write_json(
        meta_path,
        {
            "schema_version": tune.RAW_SCHEMA_VERSION,
            "page": page_number,
            "image": {"width": width, "height": height},
            "inference_params": inference_params,
            "versions": {"paddleocr": "test", "paddlepaddle": "test"},
            "files": {
                name: {"sha256": tune._file_sha256(page_dir / name)}
                for name in ("source.jpg", "layout_raw.json", "text_raw.json")
            },
        },
    )
    return meta_path


def test_import_does_not_import_paddleocr() -> None:
    assert PADDLEOCR_IMPORTED_BY_SCRIPT is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("104,105,188", [104, 105, 188]), ("3-5,4,1", [3, 4, 5, 1])],
)
def test_parse_pages(value: str, expected: list[int]) -> None:
    assert tune.parse_pages(value) == expected


@pytest.mark.parametrize("value", ["", "0", "4-2", "1,,2", "abc"])
def test_parse_pages_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        tune.parse_pages(value)


def test_layout_label_mapping() -> None:
    assert tune.map_layout_label("image") == "FIGURE"
    assert tune.map_layout_label("figure caption") == "CAPTION"
    assert tune.map_layout_label("table") == "TABLE"
    assert tune.map_layout_label("formula") == "FORMULA"
    assert tune.map_layout_label("unrecognized") == "OTHER"


def test_extracts_paddle_result_shapes_and_json_values() -> None:
    class ArrayLike:
        def tolist(self):
            return [[[1, 2], [5, 2], [5, 4], [1, 4]]]

    raw = {"res": {"dt_polys": ArrayLike(), "dt_scores": [0.8]}}
    assert tune.extract_text_lines(raw) == [
        {"bbox": [1.0, 2.0, 5.0, 4.0], "score": 0.8}
    ]
    assert tune.json_ready(ArrayLike()) == [[[1, 2], [5, 2], [5, 4], [1, 4]]]


def test_figure_internal_text_is_suppressed_from_body() -> None:
    layout = [
        {"bbox": [10, 10, 90, 90], "type": "FIGURE", "label": "image", "score": 0.9}
    ]
    lines = [
        {"bbox": [20, 20, 80, 30], "score": 0.9},
        {"bbox": [10, 100, 90, 110], "score": 0.8},
    ]
    boxes = tune.build_candidate_boxes(
        layout,
        lines,
        page_number=1,
        page_height=200,
        vertical_gap=0.02,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
    )
    assert [box["type"] for box in boxes].count("FIGURE") == 1
    body_boxes = [box for box in boxes if box["type"] == "BODY"]
    assert len(body_boxes) == 1
    assert body_boxes[0]["y0"] == 100


def test_vertical_gap_changes_text_line_aggregation() -> None:
    lines = [
        {"bbox": [10, 10, 80, 20], "score": 0.8, "type": "BODY"},
        {"bbox": [10, 27, 80, 37], "score": 0.9, "type": "BODY"},
    ]
    separated = tune.aggregate_text_lines(
        lines,
        page_height=100,
        vertical_gap=0.05,
        horizontal_overlap_threshold=0.5,
    )
    merged = tune.aggregate_text_lines(
        lines,
        page_height=100,
        vertical_gap=0.08,
        horizontal_overlap_threshold=0.5,
    )
    assert len(separated) == 2
    assert len(merged) == 1
    assert merged[0]["line_count"] == 2


def test_horizontal_overlap_changes_text_line_aggregation() -> None:
    lines = [
        {"bbox": [10, 10, 60, 20], "score": 1.0, "type": "BODY"},
        {"bbox": [40, 22, 90, 32], "score": 1.0, "type": "BODY"},
    ]
    assert (
        len(
            tune.aggregate_text_lines(
                lines,
                page_height=100,
                vertical_gap=0.05,
                horizontal_overlap_threshold=0.3,
            )
        )
        == 1
    )
    assert (
        len(
            tune.aggregate_text_lines(
                lines,
                page_height=100,
                vertical_gap=0.05,
                horizontal_overlap_threshold=0.5,
            )
        )
        == 2
    )


def test_reading_order_and_stable_ids() -> None:
    boxes = [
        {"id": "b", "type": "BODY", "x0": 50, "y0": 10, "x1": 90, "y1": 20, "score": 1},
        {
            "id": "a",
            "type": "FIGURE",
            "x0": 10,
            "y0": 10,
            "x1": 40,
            "y1": 30,
            "score": 1,
        },
        {
            "id": "c",
            "type": "FOOTNOTE",
            "x0": 10,
            "y0": 80,
            "x1": 90,
            "y1": 90,
            "score": 1,
        },
    ]
    ordered = tune.assign_reading_order(boxes)
    assert [box["id"] for box in ordered] == ["a", "b", "c"]
    assert [box["reading_order"] for box in ordered] == [1, 2, 3]

    overridden = tune.apply_reading_order(ordered, ["c", "a", "b"])
    assert [box["id"] for box in overridden] == ["c", "a", "b"]
    assert [box["reading_order"] for box in overridden] == [1, 2, 3]
    assert overridden[1]["x0"] == 10


def test_reading_order_override_requires_exact_ids() -> None:
    boxes = [
        {"id": "a", "type": "BODY", "x0": 0, "y0": 0, "x1": 1, "y1": 1},
        {"id": "b", "type": "BODY", "x0": 0, "y0": 2, "x1": 1, "y1": 3},
    ]
    with pytest.raises(tune.TuneError, match="missing=.*b"):
        tune.apply_reading_order(boxes, ["a"])


def test_candidate_schema_and_annotated_jpeg(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    doc = fitz.open()
    page = doc.new_page(width=120, height=160)
    page.draw_rect(page.rect, fill=(1, 1, 1))
    source.write_bytes(page.get_pixmap(alpha=False).tobytes("jpeg", jpg_quality=82))
    doc.close()
    boxes = [
        {
            "id": "p0001-body-deadbeef00",
            "type": "BODY",
            "x0": 10.0,
            "y0": 20.0,
            "x1": 100.0,
            "y1": 60.0,
            "score": 0.9,
            "reading_order": 1,
        }
    ]
    output = tmp_path / "annotated.jpg"
    tune.draw_annotated_jpeg(source, output, boxes, quality=70)
    assert output.read_bytes().startswith(b"\xff\xd8")
    rendered = fitz.Pixmap(str(output))
    assert (rendered.width, rendered.height) == (120, 160)

    candidate = {"boxes": boxes, "coordinate_space": {"source": "PDF CropBox"}}
    candidate_path = tmp_path / "candidate.json"
    tune._write_json(candidate_path, candidate)
    restored = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert restored["boxes"][0]["reading_order"] == 1
    assert restored["coordinate_space"]["source"] == "PDF CropBox"


@pytest.mark.parametrize(("rotation", "expected"), [(0, (200, 300)), (90, (300, 200))])
def test_render_cropbox_uses_page_rect_for_offset_and_rotation(
    tmp_path: Path, rotation: int, expected: tuple[int, int]
) -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.set_cropbox(fitz.Rect(50, 60, 250, 360))
    page.set_rotation(rotation)
    output = tmp_path / f"crop-{rotation}.jpg"

    dimensions = tune.render_cropbox_jpeg(page, output, dpi=72, quality=82)

    assert dimensions == expected
    rendered = fitz.Pixmap(str(output))
    assert (rendered.width, rendered.height) == expected
    document.close()


def test_reuse_raw_reports_parameter_mismatch(tmp_path: Path) -> None:
    meta = _write_reuse_artifacts(
        tmp_path,
        {"dpi": 150, "layout_threshold": 0.35},
        page_number=104,
    )
    with pytest.raises(tune.TuneError, match="raw parameters do not match.*dpi"):
        tune._validate_reuse_meta(meta, {"dpi": 200}, 104)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 1, "raw schema mismatch"),
        ("page", 2, "raw page mismatch"),
    ],
)
def test_reuse_rejects_schema_and_page_mismatch(
    tmp_path: Path, field: str, value: int, message: str
) -> None:
    expected = {"dpi": 150}
    meta_path = _write_reuse_artifacts(tmp_path, expected)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta[field] = value
    tune._write_json(meta_path, meta)

    with pytest.raises(tune.TuneError, match=message):
        tune._validate_reuse_meta(meta_path, expected, 1)


def test_reuse_rejects_rendering_dimension_mismatch(tmp_path: Path) -> None:
    expected = {"dpi": 150}
    meta_path = _write_reuse_artifacts(tmp_path, expected)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["image"]["width"] = 101
    tune._write_json(meta_path, meta)

    with pytest.raises(tune.TuneError, match="dimensions do not match"):
        tune._validate_reuse_meta(meta_path, expected, 1)


@pytest.mark.parametrize("filename", ["source.jpg", "layout_raw.json", "text_raw.json"])
def test_reuse_rejects_corrupt_bound_file(tmp_path: Path, filename: str) -> None:
    expected = {"dpi": 150}
    meta_path = _write_reuse_artifacts(tmp_path, expected)
    (tmp_path / filename).write_bytes(b"corrupt")

    with pytest.raises(tune.TuneError, match=f"hash mismatch.*{filename}"):
        tune._validate_reuse_meta(meta_path, expected, 1)


def test_reuse_rejects_mixed_raw_files(tmp_path: Path) -> None:
    expected = {"dpi": 150}
    meta_path = _write_reuse_artifacts(tmp_path, expected)
    layout_path = tmp_path / "layout_raw.json"
    layout_path.write_bytes((tmp_path / "text_raw.json").read_bytes())

    with pytest.raises(tune.TuneError, match="hash mismatch.*layout_raw.json"):
        tune._validate_reuse_meta(meta_path, expected, 1)


def test_json_writer_uses_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = tune.os.replace

    def tracked_replace(source, destination) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(tune.os, "replace", tracked_replace)
    output = tmp_path / "raw.json"
    tune._write_json(output, {"ok": True})

    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
    assert len(replacements) == 1
    assert replacements[0][1] == output
    assert replacements[0][0].parent == output.parent
    assert not replacements[0][0].exists()


def _model_args(tmp_path: Path):
    return tune.build_parser().parse_args(
        ["--pdf", str(tmp_path / "book.pdf"), "--pages", "1", "--output", str(tmp_path)]
    )


def test_model_construction_error_has_context_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_paddleocr = ModuleType("paddleocr")

    class FailingLayout:
        def __init__(self, **_kwargs):
            raise ValueError("bad layout setup")

    class UnusedText:
        pass

    fake_paddleocr.__dict__["LayoutDetection"] = FailingLayout
    fake_paddleocr.__dict__["TextDetection"] = UnusedText
    monkeypatch.setitem(sys.modules, "paddleocr", fake_paddleocr)
    image = tmp_path / "page-0104" / "source.jpg"

    with pytest.raises(tune.TuneError) as caught:
        tune.load_models(_model_args(tmp_path), image, 104)

    message = str(caught.value)
    assert "stage=construct" in message
    assert tune.LAYOUT_MODEL in message
    assert "page=104" in message
    assert str(image) in message
    assert isinstance(caught.value.__cause__, ValueError)


def test_text_model_construction_error_names_text_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_paddleocr = ModuleType("paddleocr")

    class GoodLayout:
        def __init__(self, **_kwargs):
            pass

    class FailingText:
        def __init__(self, **_kwargs):
            raise ValueError("bad text setup")

    fake_paddleocr.__dict__["LayoutDetection"] = GoodLayout
    fake_paddleocr.__dict__["TextDetection"] = FailingText
    monkeypatch.setitem(sys.modules, "paddleocr", fake_paddleocr)
    image = tmp_path / "page-0105" / "source.jpg"

    with pytest.raises(tune.TuneError) as caught:
        tune.load_models(_model_args(tmp_path), image, 105)

    message = str(caught.value)
    assert "stage=construct" in message
    assert tune.TEXT_MODEL in message
    assert "page=105" in message
    assert str(image) in message
    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.parametrize(
    ("failing_index", "model_name"),
    [(0, tune.LAYOUT_MODEL), (1, tune.TEXT_MODEL)],
)
def test_model_prediction_error_has_context_and_cause(
    tmp_path: Path, failing_index: int, model_name: str
) -> None:
    class GoodModel:
        def predict(self, _source: str, *, batch_size: int):
            assert batch_size == 1
            return []

    class FailingModel:
        def predict(self, _source: str, *, batch_size: int):
            assert batch_size == 1
            raise ValueError("bad prediction")

    models: list[Any] = [GoodModel(), GoodModel()]
    models[failing_index] = FailingModel()
    image = tmp_path / "page-0188" / "source.jpg"

    with pytest.raises(tune.TuneError) as caught:
        tune.run_models(image, _model_args(tmp_path), 188, tuple(models))

    message = str(caught.value)
    assert "stage=predict" in message
    assert model_name in message
    assert "page=188" in message
    assert str(image) in message
    assert isinstance(caught.value.__cause__, ValueError)


def test_execute_reuse_raw_does_not_load_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "one-page.pdf"
    pdf = fitz.open()
    pdf.new_page(width=100, height=120)
    pdf.save(pdf_path)
    pdf.close()

    output = tmp_path / "run"
    page_dir = output / "page-0001"

    args = tune.build_parser().parse_args(
        [
            "--pdf",
            str(pdf_path),
            "--pages",
            "1",
            "--output",
            str(output),
            "--reuse-raw",
        ]
    )
    inference_params = tune._inference_params(args, tune._file_sha256(pdf_path))
    _write_reuse_artifacts(page_dir, inference_params)

    def fail_load_models(_args, _source_path, _page_number):
        raise AssertionError("reuse path loaded Paddle models")

    monkeypatch.setattr(tune, "load_models", fail_load_models)
    run = tune.execute(args)

    assert run["pages"][0]["reused_raw"] is True
    candidate = json.loads((page_dir / "candidate.json").read_text(encoding="utf-8"))
    assert candidate["boxes"][0]["type"] == "BODY"
    assert (page_dir / "annotated.jpg").read_bytes().startswith(b"\xff\xd8")
