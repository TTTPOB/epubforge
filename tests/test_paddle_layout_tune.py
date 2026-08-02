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


def _write_jpeg(
    path: Path,
    width: int = 100,
    height: int = 120,
    *,
    fill: tuple[float, float, float] = (1, 1, 1),
) -> None:
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(page.rect, fill=fill)
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
                    {"label": "image", "score": 0.95, "coordinate": [10, 10, 90, 40]},
                    {"label": "text", "score": 0.9, "coordinate": [10, 10, 90, 40]},
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


def test_default_keeps_figure_internal_db_text() -> None:
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
    assert len(body_boxes) == 2
    assert [box["y0"] for box in body_boxes] == [20.0, 100.0]


def test_exclude_policy_removes_figure_internal_db_text() -> None:
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
        figure_text_policy="exclude",
    )

    body_boxes = [box for box in boxes if box["type"] == "BODY"]
    assert len(body_boxes) == 1
    assert body_boxes[0]["y0"] == 100


def test_default_keeps_figure_internal_layout_text() -> None:
    layout = [
        {"bbox": [10, 10, 90, 90], "type": "FIGURE", "label": "image", "score": 0.9},
        {"bbox": [20, 20, 80, 40], "type": "BODY", "label": "text", "score": 0.8},
        {
            "bbox": [10, 100, 90, 110],
            "type": "CAPTION",
            "label": "figure_caption",
            "score": 0.8,
        },
        {
            "bbox": [0, 80, 100, 120],
            "type": "TITLE",
            "label": "title",
            "score": 0.7,
        },
        {
            "bbox": [10, 130, 90, 140],
            "type": "FOOTNOTE",
            "label": "footnote",
            "score": 0.8,
        },
    ]

    boxes = tune.build_candidate_boxes(
        layout,
        [],
        page_number=1,
        page_height=200,
        vertical_gap=0.02,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
    )

    assert [(box["type"], box["y0"]) for box in boxes] == [
        ("FIGURE", 10.0),
        ("BODY", 20.0),
        ("TITLE", 80.0),
        ("CAPTION", 100.0),
        ("FOOTNOTE", 130.0),
    ]


def test_exclude_policy_removes_only_figure_internal_layout_text() -> None:
    layout = [
        {"bbox": [10, 10, 90, 90], "type": "FIGURE", "label": "image", "score": 0.9},
        {"bbox": [20, 20, 80, 40], "type": "BODY", "label": "text", "score": 0.8},
        {"bbox": [0, 80, 100, 120], "type": "TITLE", "label": "title", "score": 0.7},
    ]
    boxes = tune.build_candidate_boxes(
        layout,
        [],
        page_number=1,
        page_height=200,
        vertical_gap=0.02,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
        figure_text_policy="exclude",
    )

    assert [(box["type"], box["y0"]) for box in boxes] == [
        ("FIGURE", 10.0),
        ("TITLE", 80.0),
    ]


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


def test_figure_obstacle_separates_side_text_from_full_width_text_below() -> None:
    lines = [
        {"bbox": [70, 10, 100, 20], "score": 0.8, "type": "BODY"},
        {"bbox": [70, 25, 100, 35], "score": 0.9, "type": "BODY"},
        {"bbox": [0, 65, 100, 75], "score": 1.0, "type": "BODY"},
        {"bbox": [0, 80, 100, 90], "score": 1.0, "type": "BODY"},
    ]
    figures = [{"bbox": [0, 0, 60, 60], "type": "FIGURE"}]

    groups = tune.aggregate_text_lines(
        lines,
        page_height=1000,
        vertical_gap=0.04,
        horizontal_overlap_threshold=0.5,
        obstacles=figures,
        obstacle_overlap_threshold=0.5,
    )

    assert [group["bbox"] for group in groups] == [
        [70, 10, 100, 35],
        [0, 65, 100, 90],
    ]
    assert [group["line_count"] for group in groups] == [2, 2]


def test_existing_small_figure_overlap_does_not_block_text_aggregation() -> None:
    lines = [
        {"bbox": [0, 10, 100, 20], "score": 0.8, "type": "BODY"},
        {"bbox": [0, 25, 100, 35], "score": 0.9, "type": "BODY"},
    ]
    figures = [{"bbox": [95, 0, 105, 20], "type": "FIGURE"}]

    groups = tune.aggregate_text_lines(
        lines,
        page_height=100,
        vertical_gap=0.1,
        horizontal_overlap_threshold=0.5,
        obstacles=figures,
        obstacle_overlap_threshold=0.5,
    )

    assert len(groups) == 1
    assert groups[0]["bbox"] == [0, 10, 100, 35]


def test_figure_obstacle_blocks_inside_to_outside_aggregation() -> None:
    lines = [
        {"bbox": [10, 10, 90, 20], "score": 0.8, "type": "BODY"},
        {"bbox": [10, 110, 90, 120], "score": 0.9, "type": "BODY"},
    ]
    figures = [{"bbox": [0, 0, 100, 100], "type": "FIGURE"}]

    groups = tune.aggregate_text_lines(
        lines,
        page_height=1000,
        vertical_gap=0.1,
        horizontal_overlap_threshold=0.5,
        obstacles=figures,
        obstacle_overlap_threshold=0.5,
    )

    assert [group["bbox"] for group in groups] == [
        [10, 10, 90, 20],
        [10, 110, 90, 120],
    ]


def test_figure_obstacle_allows_aggregation_inside_same_figure() -> None:
    lines = [
        {"bbox": [10, 10, 90, 20], "score": 0.8, "type": "BODY"},
        {"bbox": [10, 30, 90, 40], "score": 0.9, "type": "BODY"},
    ]
    figures = [{"bbox": [0, 0, 100, 100], "type": "FIGURE"}]

    groups = tune.aggregate_text_lines(
        lines,
        page_height=100,
        vertical_gap=0.2,
        horizontal_overlap_threshold=0.5,
        obstacles=figures,
        obstacle_overlap_threshold=0.5,
    )

    assert len(groups) == 1
    assert groups[0]["bbox"] == [10, 10, 90, 40]


def test_figure_obstacle_split_is_independent_from_text_policy() -> None:
    layout = [
        {"bbox": [0, 0, 60, 60], "type": "FIGURE", "label": "image", "score": 0.9}
    ]
    lines = [
        {"bbox": [70, 10, 100, 20], "score": 0.8},
        {"bbox": [70, 25, 100, 35], "score": 0.9},
        {"bbox": [0, 65, 100, 75], "score": 1.0},
        {"bbox": [0, 80, 100, 90], "score": 1.0},
    ]
    common = {
        "page_number": 1,
        "page_height": 1000,
        "vertical_gap": 0.04,
        "horizontal_overlap_threshold": 0.5,
        "caption_gap": 0.0,
        "figure_text_overlap": 0.5,
        "figure_text_policy": "keep",
        "candidate_source": "db-only",
    }

    unsplit = tune.build_candidate_boxes(layout, lines, **common)
    split = tune.build_candidate_boxes(
        layout, lines, **common, figure_obstacle_split=True
    )

    assert len(unsplit) == 1
    assert len(split) == 2
    assert all(box["source"] == "db_group" for box in split)


@pytest.mark.parametrize(
    ("candidate_source", "sources", "types"),
    [
        ("combined", ["layout", "db_group"], ["FIGURE", "BODY"]),
        ("layout-only", ["layout", "layout"], ["FIGURE", "BODY"]),
        ("db-only", ["db_group"], ["BODY"]),
    ],
)
def test_candidate_source_modes(
    candidate_source: str, sources: list[str], types: list[str]
) -> None:
    layout = [
        {"bbox": [0, 0, 20, 20], "type": "FIGURE", "label": "image", "score": 0.9},
        {"bbox": [30, 30, 90, 60], "type": "BODY", "label": "text", "score": 0.8},
    ]
    lines = [{"bbox": [30, 35, 90, 45], "score": 0.7}]

    boxes = tune.build_candidate_boxes(
        layout,
        lines,
        page_number=1,
        page_height=100,
        vertical_gap=0.02,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
        candidate_source=candidate_source,
    )

    assert [box["source"] for box in boxes] == sources
    assert [box["type"] for box in boxes] == types


def test_combined_db_candidate_retains_layout_and_db_evidence() -> None:
    layout = [
        {
            "bbox": [10, 10, 90, 40],
            "type": "BODY",
            "label": "text",
            "score": 0.9,
            "evidence_id": "L001",
        }
    ]
    lines = [
        {"bbox": [12, 12, 88, 20], "score": 0.8, "evidence_id": "D002"},
        {"bbox": [12, 22, 88, 30], "score": 0.8, "evidence_id": "D001"},
    ]

    boxes = tune.build_candidate_boxes(
        layout,
        lines,
        page_number=1,
        page_height=100,
        vertical_gap=0.1,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
        figure_text_policy="keep",
        candidate_source="combined",
    )

    assert len(boxes) == 1
    assert boxes[0]["source"] == "db_group"
    assert boxes[0]["source_evidence_ids"] == ["D001", "D002", "L001"]


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


def test_stable_id_uses_full_float_precision_and_normalizes_negative_zero() -> None:
    first = tune._stable_id(1, "BODY", [0.001, 1.0, 2.0, 3.0])
    second = tune._stable_id(1, "BODY", [0.002, 1.0, 2.0, 3.0])

    assert first != second
    assert tune._stable_id(1, "BODY", [-0.0, 1.0, 2.0, 3.0]) == tune._stable_id(
        1, "BODY", [0.0, 1.0, 2.0, 3.0]
    )


def test_build_candidates_deduplicates_equal_geometry_and_merges_evidence() -> None:
    layout = [
        {
            "bbox": [10, 10, 90, 90],
            "type": "FIGURE",
            "label": "image",
            "score": 0.8,
            "evidence_id": "L001",
        },
        {
            "bbox": [10, 10, 90, 90],
            "type": "FIGURE",
            "label": "image",
            "score": 0.9,
            "evidence_id": "L002",
        },
    ]

    boxes = tune.build_candidate_boxes(
        layout,
        [],
        page_number=1,
        page_height=100,
        vertical_gap=0.02,
        horizontal_overlap_threshold=0.5,
        caption_gap=0.02,
        figure_text_overlap=0.5,
        candidate_source="layout-only",
    )

    assert len(boxes) == 1
    assert boxes[0]["source_evidence_ids"] == ["L001", "L002"]
    assert boxes[0]["sources"] == ["layout"]
    assert boxes[0]["score"] == 0.9


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


def test_annotated_candidate_uses_fixed_visual_contract() -> None:
    class RecordingPage:
        def __init__(self) -> None:
            self.rects: list[tuple[fitz.Rect, dict[str, Any]]] = []
            self.text: list[tuple[tuple[float, float], str, dict[str, Any]]] = []

        def draw_rect(self, rect: fitz.Rect, **kwargs: Any) -> None:
            self.rects.append((rect, kwargs))

        def insert_text(
            self, origin: tuple[float, float], text: str, **kwargs: Any
        ) -> None:
            self.text.append((origin, text, kwargs))

    page = RecordingPage()
    tune._draw_annotated_candidate(
        page,  # type: ignore[arg-type]
        {
            "id": "p0001-body-deadbeef00",
            "type": "BODY",
            "x0": 40.0,
            "y0": 100.0,
            "x1": 180.0,
            "y1": 150.0,
            "reading_order": 1,
        },
        page_width=400.0,
        page_height=300.0,
    )

    candidate_rect, candidate_kwargs = page.rects[0]
    label_rect, label_kwargs = page.rects[1]
    assert candidate_rect == fitz.Rect(40.0, 100.0, 180.0, 150.0)
    assert candidate_kwargs == {"color": tune._COLORS["BODY"], "width": 4.0}
    assert label_kwargs == {
        "color": None,
        "fill": tune._COLORS["BODY"],
        "fill_opacity": 0.5,
        "width": 0,
    }
    assert len(page.text) == 1
    _, text, text_kwargs = page.text[0]
    assert text == "1 p0001-body-deadbeef00 BODY"
    assert text_kwargs == {
        "fontname": "helv",
        "fontsize": 12.0,
        "color": tune.ANNOTATED_LABEL_WHITE,
    }
    assert label_rect.width > 0
    assert label_rect.height > 0


def test_annotated_jpeg_preserves_dimensions_and_blends_label_fill(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jpg"
    output = tmp_path / "annotated.jpg"
    _write_jpeg(source, width=400, height=300)
    box = {
        "id": "p0001-body-deadbeef00",
        "type": "BODY",
        "x0": 40.0,
        "y0": 100.0,
        "x1": 180.0,
        "y1": 150.0,
        "reading_order": 1,
    }

    tune.draw_annotated_jpeg(source, output, [box], quality=100)

    geometry = tune._label_geometry(
        "1 p0001-body-deadbeef00 BODY",
        fitz.Rect(40.0, 100.0, 180.0, 150.0),
        page_width=400.0,
        page_height=300.0,
    )
    assert geometry is not None
    label_rect = geometry[0]
    rendered = fitz.Pixmap(str(output))
    assert (rendered.width, rendered.height) == (400, 300)
    fill_pixel = rendered.pixel(int(label_rect.x0 + 1), int(label_rect.y0 + 1))
    assert fill_pixel[1] < 230
    assert fill_pixel[1] > fill_pixel[0]
    assert all(channel > 230 for channel in rendered.pixel(300, 250))


@pytest.mark.parametrize(
    "candidate_rect",
    [
        fitz.Rect(10.0, 0.0, 90.0, 20.0),
        fitz.Rect(230.0, 30.0, 240.0, 55.0),
        fitz.Rect(20.0, 275.0, 80.0, 300.0),
    ],
)
def test_label_geometry_stays_inside_page_edges(candidate_rect: fitz.Rect) -> None:
    geometry = tune._label_geometry(
        "1 p0001-body-deadbeef00 BODY",
        candidate_rect,
        page_width=240.0,
        page_height=300.0,
    )

    assert geometry is not None
    label_rect, origin, font_size = geometry
    assert 0.0 <= label_rect.x0
    assert label_rect.x1 <= 240.0
    assert 0.0 <= label_rect.y0
    assert label_rect.y1 <= 300.0
    assert label_rect.x0 <= origin[0] < label_rect.x1
    assert label_rect.y0 < origin[1] <= label_rect.y1
    assert font_size == 12.0


def test_label_geometry_skips_tiny_page() -> None:
    assert (
        tune._label_geometry(
            "1 p0001-body-deadbeef00 BODY",
            fitz.Rect(0.0, 0.0, 3.0, 3.0),
            page_width=3.0,
            page_height=3.0,
        )
        is None
    )


def test_draw_annotated_jpeg_closes_document_when_rendering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.jpg"
    _write_jpeg(source)
    opened: list[Any] = []
    real_open = tune.fitz.open

    def tracked_open(*args: Any, **kwargs: Any) -> Any:
        document = real_open(*args, **kwargs)
        opened.append(document)
        return document

    monkeypatch.setattr(tune.fitz, "open", tracked_open)
    with pytest.raises(KeyError):
        tune.draw_annotated_jpeg(
            source,
            tmp_path / "annotated.jpg",
            [
                {
                    "id": "missing-reading-order",
                    "type": "BODY",
                    "x0": 1,
                    "y0": 1,
                    "x1": 2,
                    "y1": 2,
                }
            ],
            quality=90,
        )

    assert len(opened) == 1
    assert opened[0].is_closed


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


def test_new_cli_defaults(tmp_path: Path) -> None:
    args = _model_args(tmp_path)

    assert args.figure_text_policy == "keep"
    assert args.figure_obstacle_split is False
    assert args.candidate_source == "combined"
    assert args.page_patch == []


def test_new_cli_explicit_values(tmp_path: Path) -> None:
    args = tune.build_parser().parse_args(
        [
            "--pdf",
            str(tmp_path / "book.pdf"),
            "--pages",
            "1",
            "--output",
            str(tmp_path),
            "--figure-text-policy",
            "exclude",
            "--figure-obstacle-split",
            "--candidate-source",
            "db-only",
        ]
    )

    assert args.figure_text_policy == "exclude"
    assert args.figure_obstacle_split is True
    assert args.candidate_source == "db-only"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--caption-gap", "inf"),
        ("--db-unclip-ratio", "nan"),
        ("--layout-threshold", "nan"),
        ("--figure-text-overlap", "inf"),
    ],
)
def test_cli_rejects_nonfinite_float_parameters(
    tmp_path: Path, option: str, value: str
) -> None:
    parser = tune.build_parser()
    args = parser.parse_args(
        [
            "--pdf",
            str(tmp_path / "book.pdf"),
            "--pages",
            "1",
            "--output",
            str(tmp_path),
            option,
            value,
        ]
    )

    with pytest.raises(SystemExit):
        tune._validate_args(parser, args)


def test_page_patch_argument_parsing(tmp_path: Path) -> None:
    patch = tmp_path / "page.py"
    patch.write_text(
        "def patch_page(context):\n    return context['candidate_boxes']\n"
    )

    assert tune.parse_page_patch(f"105={patch}") == (105, patch.resolve())
    with pytest.raises(ValueError, match="PAGE=PATH"):
        tune.parse_page_patch("105")
    with pytest.raises(ValueError, match="does not exist"):
        tune.parse_page_patch(f"105={tmp_path / 'missing.py'}")


@pytest.mark.parametrize(
    ("pages", "patch_pages", "message"),
    [([1], [1, 1], "duplicate page patch"), ([1], [2], "unrequested page")],
)
def test_page_patch_rejects_duplicate_and_unrequested_pages(
    tmp_path: Path, pages: list[int], patch_pages: list[int], message: str
) -> None:
    patch = tmp_path / "page.py"
    patch.write_text(
        "def patch_page(context):\n    return context['candidate_boxes']\n"
    )

    with pytest.raises(tune.TuneError, match=message):
        tune._resolve_page_patches(
            [(page_number, patch) for page_number in patch_pages], pages
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
    body = next(box for box in candidate["boxes"] if box["type"] == "BODY")
    assert body["source"] == "db_group"
    assert body["source_evidence_ids"] == ["D001", "L002"]
    assert body["figure_relations"] == [
        {
            "figure_evidence_id": "L001",
            "overlap_ratio": 1.0,
            "contained": True,
            "meets_threshold": True,
        }
    ]
    assert candidate["schema_version"] == tune.CANDIDATE_SCHEMA_VERSION
    assert candidate["postprocess_params"] == {
        "vertical_gap": 0.012,
        "horizontal_overlap": 0.5,
        "caption_gap": 0.025,
        "figure_text_overlap": 0.5,
        "figure_text_policy": "keep",
        "figure_obstacle_split": False,
        "candidate_source": "combined",
    }
    evidence = json.loads((page_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["layout_boxes"][0]["evidence_id"] == "L001"
    assert evidence["layout_boxes"][1]["evidence_id"] == "L002"
    assert evidence["db_lines"][0]["evidence_id"] == "D001"
    assert (
        evidence["db_lines"][0]["figure_relations"][0]["figure_evidence_id"] == "L001"
    )
    assert (page_dir / "layout_raw.jpg").read_bytes().startswith(b"\xff\xd8")
    assert (page_dir / "text_raw.jpg").read_bytes().startswith(b"\xff\xd8")
    assert (page_dir / "annotated.jpg").read_bytes().startswith(b"\xff\xd8")
    assert candidate["artifacts"]["evidence"] == "evidence.json"
    assert run["pages"][0]["artifacts"]["layout_evidence_image"] == (
        "page-0001/layout_raw.jpg"
    )


def test_reuse_raw_page_patch_targets_one_page_and_records_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    pdf = fitz.open()
    pdf.new_page(width=100, height=120)
    pdf.new_page(width=100, height=120)
    pdf.save(pdf_path)
    pdf.close()
    patch_path = tmp_path / "page-2-patch.py"
    patch_path.write_text(
        """def patch_page(context):
    assert context["page"] == 2
    assert context["image"] == {"width": 100, "height": 120}
    assert context["layout_evidence"][0]["evidence_id"] == "L001"
    assert context["db_evidence"][0]["evidence_id"] == "D001"
    assert context["normalized_layout_boxes"][0]["evidence_id"] == "L001"
    assert context["normalized_text_lines"][0]["evidence_id"] == "D001"
    assert context["postprocess_params"]["candidate_source"] == "combined"
    context["layout_evidence"][0]["label"] = "mutated-copy"
    body = next(box for box in context["candidate_boxes"] if box["type"] == "BODY")
    body["type"] = "TITLE"
    return [body]
""",
        encoding="utf-8",
    )
    output = tmp_path / "run"
    args = tune.build_parser().parse_args(
        [
            "--pdf",
            str(pdf_path),
            "--pages",
            "1,2",
            "--output",
            str(output),
            "--reuse-raw",
            "--page-patch",
            f"2={patch_path}",
        ]
    )
    inference_params = tune._inference_params(args, tune._file_sha256(pdf_path))
    _write_reuse_artifacts(output / "page-0001", inference_params, page_number=1)
    _write_reuse_artifacts(output / "page-0002", inference_params, page_number=2)

    def fail_load_models(_args, _source_path, _page_number):
        raise AssertionError("reuse path loaded Paddle models")

    monkeypatch.setattr(tune, "load_models", fail_load_models)
    run = tune.execute(args)

    page_one = json.loads(
        (output / "page-0001" / "candidate.json").read_text(encoding="utf-8")
    )
    page_two = json.loads(
        (output / "page-0002" / "candidate.json").read_text(encoding="utf-8")
    )
    assert page_one["page_patch"] is None
    assert all(box["source"] != "page_patch" for box in page_one["boxes"])
    assert len(page_two["boxes"]) == 1
    patched = page_two["boxes"][0]
    assert patched["type"] == "TITLE"
    assert patched["source"] == "page_patch"
    assert patched["id"] == tune._stable_id(
        2, "TITLE", [patched["x0"], patched["y0"], patched["x1"], patched["y1"]]
    )
    expected_record = {
        "path": str(patch_path.resolve()),
        "sha256": tune._file_sha256(patch_path),
    }
    assert page_two["page_patch"] == expected_record
    assert run["pages"][1]["page_patch"] == expected_record
    assert run["page_patches"] == [{"page": 2, **expected_record}]
    evidence = json.loads(
        (output / "page-0002" / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["layout_boxes"][0]["label"] == "image"


@pytest.mark.parametrize(
    ("patch_source", "message"),
    [
        ("def patch_page(context):\n    return {'bad': True}\n", "expected a list"),
        (
            """def patch_page(context):
    return [{'type': 'UNKNOWN', 'x0': 0, 'y0': 0, 'x1': 10, 'y1': 10}]
""",
            "unsupported type",
        ),
        (
            """def patch_page(context):
    return [{'type': 'BODY', 'x0': 0, 'y0': 0, 'x1': 101, 'y1': 10,
             'source_evidence_ids': ['D001']}]
""",
            "outside the CropBox",
        ),
    ],
)
def test_page_patch_rejects_invalid_return_and_bounds(
    tmp_path: Path, patch_source: str, message: str
) -> None:
    patch_path = tmp_path / "invalid-patch.py"
    patch_path.write_text(patch_source, encoding="utf-8")

    with pytest.raises(tune.TuneError, match=message):
        tune.apply_page_patch(
            patch_path,
            page_number=1,
            width=100,
            height=120,
            evidence={
                "layout_boxes": [],
                "db_lines": [{"evidence_id": "D001"}],
            },
            layout_boxes=[],
            text_lines=[],
            candidate_boxes=[],
            postprocess_params={},
        )


def test_page_patch_exception_has_page_path_and_cause(tmp_path: Path) -> None:
    patch_path = tmp_path / "failing-patch.py"
    patch_path.write_text(
        "def patch_page(context):\n    raise RuntimeError('private detail')\n",
        encoding="utf-8",
    )

    with pytest.raises(tune.TuneError) as caught:
        tune.apply_page_patch(
            patch_path,
            page_number=7,
            width=100,
            height=120,
            evidence={"layout_boxes": [], "db_lines": []},
            layout_boxes=[],
            text_lines=[],
            candidate_boxes=[],
            postprocess_params={},
        )

    assert "page=7" in str(caught.value)
    assert str(patch_path) in str(caught.value)
    assert "private detail" not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_page_patch_deduplicates_equal_geometry_and_merges_evidence(
    tmp_path: Path,
) -> None:
    patch_path = tmp_path / "duplicate-patch.py"
    patch_path.write_text(
        """def patch_page(context):
    return [
        {'type': 'BODY', 'x0': 1, 'y0': 2, 'x1': 20, 'y1': 10,
         'source_evidence_ids': ['D001']},
        {'type': 'BODY', 'x0': 1, 'y0': 2, 'x1': 20, 'y1': 10,
         'source_evidence_ids': ['D002']},
    ]
""",
        encoding="utf-8",
    )

    boxes, _record = tune.apply_page_patch(
        patch_path,
        page_number=1,
        width=100,
        height=120,
        evidence={
            "layout_boxes": [],
            "db_lines": [{"evidence_id": "D001"}, {"evidence_id": "D002"}],
        },
        layout_boxes=[],
        text_lines=[],
        candidate_boxes=[],
        postprocess_params={},
    )

    assert len(boxes) == 1
    assert boxes[0]["source_evidence_ids"] == ["D001", "D002"]
    assert boxes[0]["source"] == "page_patch"
    assert boxes[0]["sources"] == ["page_patch"]


def test_page_patch_hashes_and_executes_the_same_source_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_path = tmp_path / "single-read-patch.py"
    original_source = b"""def patch_page(context):
    return [{'type': 'BODY', 'x0': 1, 'y0': 2, 'x1': 20, 'y1': 10,
             'source_evidence_ids': ['D001']}]
"""
    replacement_source = b"""def patch_page(context):
    raise RuntimeError('replacement executed')
"""
    patch_path.write_bytes(original_source)
    real_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def replace_after_read(path: Path) -> bytes:
        source = real_read_bytes(path)
        if path == patch_path:
            reads.append(path)
            path.write_bytes(replacement_source)
        return source

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    boxes, record = tune.apply_page_patch(
        patch_path,
        page_number=1,
        width=100,
        height=120,
        evidence={
            "layout_boxes": [],
            "db_lines": [{"evidence_id": "D001"}],
        },
        layout_boxes=[],
        text_lines=[],
        candidate_boxes=[],
        postprocess_params={},
    )

    assert len(boxes) == 1
    assert reads == [patch_path]
    assert record == {
        "path": str(patch_path),
        "sha256": tune.hashlib.sha256(original_source).hexdigest(),
    }
    assert tune._file_sha256(patch_path) != record["sha256"]


@pytest.mark.parametrize(
    ("provenance", "message"),
    [
        ("'sources': 'layout', 'source_evidence_ids': ['D001']", "sources must"),
        (
            "'sources': ['unknown'], 'source_evidence_ids': ['D001']",
            "sources contain an unknown value",
        ),
        ("'source_evidence_ids': 'D001'", "non-empty list"),
        ("'sources': []", "non-empty list"),
        ("'source_evidence_ids': ['D999']", "unknown evidence IDs"),
    ],
)
def test_page_patch_rejects_invalid_provenance(
    tmp_path: Path, provenance: str, message: str
) -> None:
    patch_path = tmp_path / "invalid-provenance.py"
    patch_path.write_text(
        "def patch_page(context):\n"
        "    return [{'type': 'BODY', 'x0': 1, 'y0': 2, 'x1': 20, 'y1': 10, "
        f"{provenance}}}]\n",
        encoding="utf-8",
    )

    with pytest.raises(tune.TuneError, match=message):
        tune.apply_page_patch(
            patch_path,
            page_number=1,
            width=100,
            height=120,
            evidence={
                "layout_boxes": [],
                "db_lines": [{"evidence_id": "D001"}],
            },
            layout_boxes=[],
            text_lines=[],
            candidate_boxes=[],
            postprocess_params={},
        )


def test_page_patch_recomputes_figure_relations_from_evidence(tmp_path: Path) -> None:
    patch_path = tmp_path / "relation-patch.py"
    patch_path.write_text(
        """def patch_page(context):
    return [{'type': 'BODY', 'x0': 10, 'y0': 10, 'x1': 20, 'y1': 20,
             'source_evidence_ids': ['D001'],
             'figure_relations': [{'figure_evidence_id': 'fake'}]}]
""",
        encoding="utf-8",
    )

    boxes, _record = tune.apply_page_patch(
        patch_path,
        page_number=1,
        width=100,
        height=120,
        evidence={
            "layout_boxes": [
                {
                    "evidence_id": "L001",
                    "type": "FIGURE",
                    "bbox": [0, 0, 50, 50],
                }
            ],
            "db_lines": [{"evidence_id": "D001"}],
        },
        layout_boxes=[],
        text_lines=[],
        candidate_boxes=[],
        postprocess_params={"figure_text_overlap": 0.5},
    )

    assert boxes[0]["sources"] == ["page_patch"]
    assert boxes[0]["source_evidence_ids"] == ["D001"]
    assert boxes[0]["figure_relations"] == [
        {
            "figure_evidence_id": "L001",
            "overlap_ratio": 1.0,
            "contained": True,
            "meets_threshold": True,
        }
    ]


def test_candidate_deduplication_rejects_string_provenance() -> None:
    box = {
        "id": "candidate",
        "type": "BODY",
        "x0": 1,
        "y0": 2,
        "x1": 20,
        "y1": 10,
        "source": "layout",
        "sources": "layout",
        "source_evidence_ids": "L001",
    }

    with pytest.raises(tune.TuneError, match="sources must be a list"):
        tune._deduplicate_candidate_boxes([box])
