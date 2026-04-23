"""Editor package dispatcher."""

from __future__ import annotations

from epubforge.editor.cli_support import emit_json


def main() -> int:
    emit_json(
        {
            "error": "run a concrete command module, e.g. python -m epubforge.editor.doctor <work>",
            "commands": [
                "init",
                "import-legacy",
                "doctor",
                "propose-op",
                "apply-queue",
                "acquire-lease",
                "release-lease",
                "acquire-book-lock",
                "release-book-lock",
                "run-script",
                "compact",
                "snapshot",
                "render-prompt",
            ],
        }
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
