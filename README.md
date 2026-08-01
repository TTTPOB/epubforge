# epubforge

MinerU-based PDF → EPUB conversion with agentic editing support.

Stage 1 stores the original MinerU API archive at `work/<book>/01_raw.zip` and
keeps the source PDF at `work/<book>/source/source.pdf`. Set
`EPUBFORGE_MINERU_API_KEY` or `[mineru].api_key`, then run:

```bash
uv run epubforge --config config.example.toml parse input.pdf
```

Stages 2-4 still consume the legacy `01_raw.json` boundary while downstream
MinerU archive support is pending.
