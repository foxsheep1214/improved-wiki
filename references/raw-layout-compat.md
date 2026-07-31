# Raw layout compatibility — handling 3 directory shapes

The `improved-wiki` pipeline auto-detects which of three `raw/` layouts your project uses, so you don't have to migrate files just to use the scripts. This reference explains each layout, when it appears, and how detection works.

## TL;DR

| Layout | Path shape | Example | Detected as |
|---|---|---|---|
| A. improved-wiki default | `raw/<type>/<topic>/<file>` | `raw/Book/control/Automatic Control Systems - 2007 - Kuo.pdf` | type=book |
| B. LLM Wiki app legacy | `raw/sources/<type>/<file>` | `raw/sources/oldbook/file.pdf` | type=oldbook (or "book" if folder name not in `FOLDER_TO_TEMPLATE`) |
| C. Flat (no type folder) | `raw/sources/<file>` | `raw/sources/Stoica.pdf` | type=book (silent default) |

The detection lives in `scripts/_core.py` → `detect_template_type()`. Override with the `--type=<type>` CLI flag (there is no `IMPROVED_WIKI_TYPE` env var).

## Layout A — improved-wiki default (recommended)

```
raw/
├── Book/                  ← first folder = file type (Titlecase)
│   ├── control/           ← second folder = topic sub-category
│   │   └── Automatic Control Systems - 2007 - Kuo.pdf
│   ├── radar/
│   │   └── ...
│   └── power/
│       └── ...
├── Paper/
│   ├── 01_硬件设计总体/
│   └── 02_硬件电路设计/
└── Datasheet/
    └── 01_微控制器与处理器/
```

**Why two levels**: the first level is a **file type** (which drives the Ingest template), the second level is a **topic** (which drives the destination wiki sub-folder and provides sort order). See `references/naming-conventions.md` §1.2 for the full convention.

**Detection**: `parts[0]` is checked against `FOLDER_TO_TEMPLATE`. If matched, use that as the template type.

## Layout B — LLM Wiki app legacy (NashSU app default)

```
raw/
├── sources/               ← wrapper folder
│   ├── book/              ← type
│   │   └── X.pdf
│   ├── paper/
│   │   └── Y.pdf
│   └── datasheet/
│       └── Z.pdf
├── assets/                ← non-PDF assets (NOT a recognized wrapper — see note below)
│   └── ...
```

**When it appears**: any project that started life in the NashSU LLM Wiki desktop app, which uses `raw/sources/<type>/<file>` as its standard.

**Detection**: `detect_template_type` special-cases `parts[0] == "sources"` only. When `parts[1]` is present and in `FOLDER_TO_TEMPLATE`, it becomes the type; otherwise the function returns `digest-book` (Layout C).

**`sources` is the only recognized wrapper folder** — it is hardcoded as the `if folder == "sources"` branch in `_core.py::detect_template_type` (there is **no** `WRAPPER_FOLDERS` set). `assets/` or any other wrapper name is NOT skipped: it is treated as an unknown type folder and fuzzy-matched to the nearest known type (likely mislabeled, with a `[detect]` warning). If your project uses a different wrapper (e.g. `raw/inputs/`, `raw/inbox/`), you have two options:
1. Rename the folder to `sources` (one-time)
2. Add a branch for it in `detect_template_type` in `_core.py` (one-line code change)

## Layout C — Flat (no type folder)

```
raw/
├── sources/
│   ├── Spectral Analysis of Signals - 2005 - Stoica.pdf
│   ├── Automatic Control Systems - 2007 - Kuo.pdf
│   └── ... (50+ books, all flat)
```

**When it appears**: typically an early-stage project that hasn't been categorized yet, or a project where the user doesn't want categorization (everything is "books" or "papers" semantically).

**Detection**: `parts[0]` is `sources`, but there is no `parts[1]` type folder (just a file), so `detect_template_type` **silently returns `book`** — no warning is printed for this case.

**Override per-file**: pass `--type=paper` (or whichever) on the command line. There is no global env-var override (`IMPROVED_WIKI_TYPE` does not exist); `--type` is per-run.

## Adding a new layout

If your project uses a layout the script doesn't handle, edit `FOLDER_TO_TEMPLATE` in `scripts/_core.py` (values are template names **without** the `.md` suffix — `load_template` appends it):

```python
FOLDER_TO_TEMPLATE = {
    "Book": "digest-book",
    "Paper": "digest-paper",
    "Datasheet": "digest-datasheet",
    "Applicationnote": "digest-applicationnote",
    "Designexample": "digest-designexample",
    "Presentation": "digest-presentation",
    "Standard": "digest-standard",
    "News": "digest-news",
    "myCustomType": "digest-mycustom",   # ← add yours here
}
```

To recognize a new wrapper folder (beyond `sources`), add a branch to `detect_template_type` in `_core.py` — there is no `WRAPPER_FOLDERS` set to edit.

If you add a new file type, you also need to add a `templates/digest-<type>.md` (model it on the existing templates in the skill's `templates/` directory).

## Why this matters

Most "this tool doesn't work with my setup" failures come from layout mismatch. By handling 3 layouts out of the box (the most common shapes from NashSU app + improved-wiki conventions + uncategorized projects), the script gets out of the way and lets you focus on the content.

The default to "book" for flat layouts is a deliberate choice — it matches the most common case (academic books in a knowledge base) and is easy to override. The alternative (failing loudly on any unrecognized layout) would block users who haven't yet organized their `raw/` and just want to try the pipeline.

## See also

- `SKILL.md` — `raw/` convention + per-type folder meaning
- `references/initial-setup.md` — 3 worked scenarios including retrofitting an LLM Wiki app project
