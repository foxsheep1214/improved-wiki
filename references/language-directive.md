# Output Language Directive

The knowledge base has two automatic output languages: Chinese sources produce
Chinese pages; every other detected source language produces English pages.
Set `IMPROVED_WIKI_OUTPUT_LANGUAGE` to override this per-source choice for the
whole project. Proper names, technical identifiers, URLs, filenames, and paper
titles remain unchanged in either mode.

`build_language_directive()` injects the mandatory language block into Stage
2.2, Stage 2.4, and semantic-lint prompts. Deterministic media boilerplate and
write-time checks use the same `get_output_language()` decision.

## Detection guards

`detect_language()` is deliberately conservative for technical material:

- sparse non-Latin characters cannot outvote a predominantly ASCII document;
- Japanese requires a meaningful kana share rather than one borrowed token;
- Latin-language detection requires multiple unambiguous function words;
- collision-prone tokens such as radar acronyms, math variables, and common
  component abbreviations are excluded from language evidence.

Regression coverage lives in `scripts/tests/test_language.py`.

## Configuration

```bash
# auto is the default; Chinese or English forces the whole project.
export IMPROVED_WIKI_OUTPUT_LANGUAGE=auto
```

See `initial-setup.md` for bootstrap configuration.

## Retrieval impact

The default bge-m3 embedding model supports multilingual retrieval, but keyword
search and cross-language dedup remain lexical. Keeping automatic output to
Chinese or English reduces language-fragmented near-duplicates. Projects that
require one language throughout should set the override explicitly.
