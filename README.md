# epubforge

MinerU-based PDF → EPUB conversion with agentic editing support.

The default workflow produces corrected chapter HTML in five stages. Stage 1
stores the MinerU API archive at `work/<book>/01_raw.zip` and keeps the source
PDF at `work/<book>/source/source.pdf`. Stage 1 reads the full PDF page count
before any MinerU API request, accepts at most 1000 pages, and sends at most
200 pages per MinerU upload. Set
`EPUBFORGE_MINERU_API_KEY` or `[mineru].api_key`, then run:

```bash
uv run epubforge --config config.example.toml run input.pdf
```

Stage 1 requires a POSIX runtime and a local filesystem that supports durable
directory `fsync` and atomic rename. The command rejects unsupported runtimes
before it calls MinerU or changes published Stage 1 files. Network filesystems
can expose different `fsync` and rename behavior, so epubforge does not extend
the crash-consistency guarantee to them.

The default stages and outputs are:

| Stage | Command | Output |
|---|---|---|
| 1 | `parse` | `source/source.pdf`, `01_raw.zip` |
| 2 | `normalize` | `02_content/content.json`, normalized assets |
| 3 | `segment` | `03_chapters/chapters.json` |
| 4 | `prepare` | `04_edit/manifest.json`, chapter HTML and page JPEGs |
| 5 | `revise` | `04_edit/chapters/*/corrected.html`, `revision.json` |

`run` executes these stages in order. Each stage also accepts `-f` or
`--force-rerun`. `--from` accepts `1-5`; earlier stages ensure prerequisites,
and `--force-rerun` affects the selected stage and later stages. The default
run command has no page filter.

The chapter stages record the configured model in their artifacts. The example
configuration selects Luna Medium with `openai/gpt-5.6-luna` in `[llm].model`;
provider-specific reasoning or variant settings belong in `[llm].extra_body`.
The pipeline does not hardcode a model name.

The older `classify`, `extract`, `assemble`, and `build` commands remain
available as legacy interfaces while the migration finishes. They do not run
as part of `run`.

For PDFs up to 200 pages, `01_raw.zip` remains the original MinerU response ZIP.
For PDFs from 201 through 1000 pages, `01_raw.zip` is an outer ZIP with stable
member ordering and headers. It contains `manifest.json` and ordered
`segments/segment-NNN-pages-FFFF-LLLL.zip` members. Each segment member
preserves one complete MinerU response ZIP without flattening its files. Batch
IDs and response hashes in the manifest vary between MinerU runs.

## Tests

Run the test suite with two worker processes by default:

```bash
uv run pytest
```

Run the suite serially when debugging shared process state:

```bash
uv run pytest -n 0
```

Use serial mode for interactive `pdb` debugging:

```bash
uv run pytest -n 0 --pdb
```

Use four workers as an explicit local override on machines with enough CPU and memory:

```bash
uv run pytest -n 4 --dist worksteal
```
