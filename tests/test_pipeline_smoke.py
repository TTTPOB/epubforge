"""Optional end-to-end smoke test for the current five-stage run command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


FIXTURE = Path(__file__).parents[1] / "fixtures" / "bmsf.pdf"


@pytest.mark.skipif(not FIXTURE.is_file(), reason="No PDF fixture found")
@pytest.mark.skipif(
    not os.environ.get("EPUBFORGE_MINERU_API_KEY"),
    reason="EPUBFORGE_MINERU_API_KEY is required",
)
@pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="a usable opencode executable is required",
)
@pytest.mark.skipif(
    os.environ.get("EPUBFORGE_RUN_REAL_AGENT_SMOKE") != "1",
    reason="set EPUBFORGE_RUN_REAL_AGENT_SMOKE=1 for a configured OpenCode run",
)
def test_full_pipeline_smoke_uses_isolated_run_path(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    environment = os.environ.copy()
    environment.update(
        {
            "EPUBFORGE_RUNTIME_WORK_DIR": str(work_root),
        }
    )

    result = subprocess.run(
        ["uv", "run", "epubforge", "run", str(FIXTURE), "--force-rerun"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, f"Pipeline failed:\n{result.stdout}\n{result.stderr}"
    work_dir = work_root / FIXTURE.stem
    assert (work_dir / "01_raw.zip").is_file()
    assert (work_dir / "02_content/content.json").is_file()
    assert (work_dir / "03_chapters/chapters.json").is_file()
    assert (work_dir / "04_edit/manifest.json").is_file()

    chapters = json.loads(
        (work_dir / "04_edit/manifest.json").read_text(encoding="utf-8")
    )["chapters"]
    assert chapters
    for chapter in chapters:
        chapter_dir = work_dir / "04_edit" / chapter["path"]
        assert (chapter_dir / "corrected.html").is_file()
        assert (chapter_dir / "revision.json").is_file()
