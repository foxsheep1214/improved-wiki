"""Image captions follow the wiki's output language (NashSU v0.6.10 parity).

Before 0.6.10 NashSU captioned in whatever language the surrounding source
text used, and improved-wiki mirrored that ("language-NEUTRAL"). 0.6.10
changed it: captions are written in the configured output language, and the
caption cache key carries that language so a language switch re-captions
instead of serving a stale foreign-language description.

That matters here because ``get_output_language`` collapses every source to
Chinese or English. A German source therefore produced an English page body
with German alt text on every figure — two languages on one page. Text
printed INSIDE the image stays verbatim in its original language either way.

improved-wiki's caption cache is a ``<image>.caption.txt`` sidecar rather
than a hash-keyed store, so the language dimension lives in a per-media-dir
marker: a recorded language that differs from the current one makes every
caption in that directory pending again.

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_1_3_caption as caption  # noqa: E402
from _language import OUTPUT_LANGUAGE_ENV  # noqa: E402

_EN_CTX = {
    "a1b2c3d4": {
        "context_before": "The receiver front end is shown below. Its noise "
                          "figure dominates the overall sensitivity budget.",
        "context_after": "Gain is distributed across the two stages so that "
                         "the second stage never limits linearity.",
        "mineru_caption": "Figure 2.4 Receiver front-end block diagram",
    },
}
_ZH_CTX = {
    "a1b2c3d4": {
        "context_before": "下图给出接收前端的组成。其噪声系数决定了整机灵敏度预算。",
        "context_after": "增益在两级之间分配，使第二级不会成为线性度瓶颈。",
        "mineru_caption": "图2.4 接收前端框图",
    },
}


class _EnvGuard:
    """Restore OUTPUT_LANGUAGE_ENV whatever the test does to it."""

    def __enter__(self):
        self._saved = os.environ.get(OUTPUT_LANGUAGE_ENV)
        return self

    def set(self, value: str | None):
        if value is None:
            os.environ.pop(OUTPUT_LANGUAGE_ENV, None)
        else:
            os.environ[OUTPUT_LANGUAGE_ENV] = value

    def __exit__(self, *_exc):
        self.set(self._saved)
        return False


class TestCaptionLanguageResolution(unittest.TestCase):
    def test_english_context_resolves_to_english(self):
        with _EnvGuard() as env:
            env.set("auto")
            self.assertEqual(
                caption._stage_1_3_caption_language(_EN_CTX), "English")

    def test_chinese_context_resolves_to_chinese(self):
        with _EnvGuard() as env:
            env.set("auto")
            self.assertEqual(
                caption._stage_1_3_caption_language(_ZH_CTX), "Chinese")

    def test_explicit_override_wins_over_detection(self):
        with _EnvGuard() as env:
            env.set("English")
            self.assertEqual(
                caption._stage_1_3_caption_language(_ZH_CTX), "English")

    def test_empty_context_map_falls_back_to_english(self):
        with _EnvGuard() as env:
            env.set("auto")
            self.assertEqual(caption._stage_1_3_caption_language({}), "English")


class TestCaptionPrompts(unittest.TestCase):
    def test_no_context_prompt_states_the_output_language(self):
        prompt = caption._stage_1_3_build_user_prompt(
            {"filename": "x.jpg", "page": 3}, None, "Chinese")
        self.assertIn("Write the description in Chinese", prompt)

    def test_context_prompt_states_the_output_language(self):
        prompt = caption._stage_1_3_build_user_prompt(
            {"filename": "x.jpg", "page": 3},
            _EN_CTX["a1b2c3d4"], "Chinese")
        self.assertIn("Write the description in Chinese", prompt)

    def test_prompts_keep_in_image_text_verbatim(self):
        for ctx in (None, _EN_CTX["a1b2c3d4"]):
            prompt = caption._stage_1_3_build_user_prompt(
                {"filename": "x.jpg"}, ctx, "Chinese")
            self.assertIn("verbatim", prompt)
            self.assertIn("do not translate", prompt.lower())

    def test_prompts_no_longer_defer_to_the_source_language(self):
        for ctx in (None, _EN_CTX["a1b2c3d4"]):
            prompt = caption._stage_1_3_build_user_prompt(
                {"filename": "x.jpg"}, ctx, "English")
            self.assertNotIn("language of the surrounding source", prompt)

    def test_system_prompt_drops_the_language_neutral_policy(self):
        self.assertNotIn("language-NEUTRAL", caption.CAPTION_SYSTEM_PROMPT)
        self.assertNotIn("SAME language as the surrounding source text",
                         caption.CAPTION_SYSTEM_PROMPT)

    def test_system_prompt_still_protects_in_image_text(self):
        self.assertIn("VERBATIM", caption.CAPTION_SYSTEM_PROMPT)


def _captioned(media_dir: Path, name: str = "img.jpg") -> list[dict]:
    (media_dir / f"{name}.caption.txt").write_text(
        "A receiver front-end block diagram with two gain stages and a "
        "labelled noise figure budget.", encoding="utf-8")
    return [{"filename": name}]


class TestLanguageScopedCache(unittest.TestCase):
    def test_marker_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            caption._stage_1_3_write_caption_language(media, "Chinese")
            self.assertEqual(
                caption._stage_1_3_read_caption_language(media), "Chinese")

    def test_missing_marker_does_not_invalidate_existing_captions(self):
        # The installed corpora hold thousands of captions written before the
        # marker existed. Treating "no marker" as stale would re-caption all
        # of them for nothing.
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            images = _captioned(media)
            self.assertEqual(
                caption._stage_1_3_pending_images(images, media,
                                                  language="Chinese"), [])

    def test_same_language_keeps_captions_cached(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            images = _captioned(media)
            caption._stage_1_3_write_caption_language(media, "English")
            self.assertEqual(
                caption._stage_1_3_pending_images(images, media,
                                                  language="English"), [])

    def test_language_change_makes_captions_pending_again(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            images = _captioned(media)
            caption._stage_1_3_write_caption_language(media, "English")
            self.assertEqual(
                len(caption._stage_1_3_pending_images(images, media,
                                                      language="Chinese")), 1)

    def test_language_omitted_keeps_legacy_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            images = _captioned(media)
            caption._stage_1_3_write_caption_language(media, "English")
            self.assertEqual(
                caption._stage_1_3_pending_images(images, media), [])


if __name__ == "__main__":
    unittest.main()
