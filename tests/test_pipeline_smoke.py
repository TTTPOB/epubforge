"""End-to-end smoke test for the full pipeline.

Requires fixtures/*.pdf to exist and API env vars to be set.
Skips automatically when fixtures are absent so CI passes without credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

FIXTURES = sorted(Path("fixtures").glob("*.pdf"))
FIXTURE = FIXTURES[0] if FIXTURES else None


@pytest.mark.skipif(not FIXTURES, reason="No PDF fixtures found in fixtures/")
@pytest.mark.skipif(
    not os.environ.get("EPUBFORGE_LLM_API_KEY"),
    reason="EPUBFORGE_LLM_API_KEY not set",
)
def test_full_pipeline_smoke() -> None:
    """Run assemble+refine-toc+build on existing stage 3/4 outputs and verify structure."""
    assert FIXTURE is not None
    book_name = FIXTURE.stem
    work_dir = Path("work") / book_name

    # Require stages 1-4 to already exist (expensive; skip test if not)
    if not (work_dir / "02_pages.json").exists():
        pytest.skip("Stage 2 output missing — run parse+classify first")
    if not list((work_dir / "03_simple").glob("*.json")) and not list(
        (work_dir / "04_complex").glob("*.json")
    ):
        pytest.skip("Stages 3/4 outputs missing — run clean+vlm first")

    result = subprocess.run(
        ["uv", "run", "epubforge", "run", str(FIXTURE), "--from", "5", "--force-rerun"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Pipeline failed:\n{result.stdout}\n{result.stderr}"

    # Both semantic files must exist
    raw_path = work_dir / "05_semantic_raw.json"
    refined_path = work_dir / "05_semantic.json"
    assert raw_path.exists(), "05_semantic_raw.json not created"
    assert refined_path.exists(), "05_semantic.json not created"

    raw = json.loads(raw_path.read_text())
    refined = json.loads(refined_path.read_text())

    # Refined should have <= chapters than raw (refine merges misclassified level-1 headings)
    assert len(refined["chapters"]) <= len(raw["chapters"]), (
        f"Refined has more chapters ({len(refined['chapters'])}) than raw ({len(raw['chapters'])})"
    )

    # Every chapter in refined must have an id
    for ch in refined["chapters"]:
        assert ch.get("id"), f"Chapter '{ch['title']}' has no id"

    # Every inline heading in refined that has content must have an id
    for ch in refined["chapters"]:
        for block in ch.get("blocks", []):
            if block.get("kind") == "heading":
                assert block.get("id"), (
                    f"Heading '{block.get('text')}' in chapter '{ch['title']}' has no id"
                )

    # EPUB must exist and nav must contain nested <ol>
    epub_path = Path("out") / f"{book_name}.epub"
    assert epub_path.exists(), "EPUB not created"

    with zipfile.ZipFile(epub_path) as zf:
        nav_names = [n for n in zf.namelist() if "nav" in n.lower() and n.endswith(".xhtml")]
        assert nav_names, "No nav.xhtml found in EPUB"
        nav_content = zf.read(nav_names[0]).decode("utf-8")

    # Nested TOC: at least one <ol> inside a <li> (second-level nesting)
    assert nav_content.count("<ol>") >= 2, (
        "nav.xhtml does not appear to have nested TOC (expected ≥2 <ol> elements)"
    )
