"""Stage 1.3 caption provider failover (2026-07-08).

Primary provider (recommended: GLM-5v-turbo, cloud) tries first; only on
primary exhaustion does a configured fallback (recommended: local Ollama
qwen3-vl:8b-instruct) get tried. This is provider failover between two real
VLM captioners — loud (logged), not the no-silent-fallback policy's target
(silently degrading to a non-caption path). See _stage_1_3_caption.py header.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _core
import _stage_1_3_caption as cap


def _make_config(tmp: Path, **overrides) -> _core.Config:
    kwargs = dict(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic",
        caption_api_key="glm-key", caption_base_url="https://open.bigmodel.cn/api",
        caption_model="glm-5v-turbo", caption_protocol="anthropic",
        chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )
    kwargs.update(overrides)
    return _core.Config(**kwargs)


class TestLoadCaptionProviderFallback(unittest.TestCase):
    def _write_config(self, tmp: Path, obj: dict) -> None:
        agents_dir = tmp / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "config.json").write_text(json.dumps(obj), encoding="utf-8")

    def _load_with_home(self, tmp: Path) -> dict:
        orig_home = Path.home
        try:
            _core.Path.home = staticmethod(lambda: tmp)
            return _core.load_caption_provider()
        finally:
            _core.Path.home = orig_home

    def test_no_fallback_configured(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_config(tmp, {
                "caption_provider": "glm",
                "providers": {"glm": {"api_key": "k", "base_url": "https://glm",
                                       "protocol": "anthropic",
                                       "models": {"caption": "glm-5v-turbo"}}},
            })
            result = self._load_with_home(tmp)
            self.assertEqual(result["provider"], "glm")
            self.assertIsNone(result["fallback"])

    def test_fallback_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_config(tmp, {
                "caption_provider": "glm",
                "caption_fallback_provider": "ollama",
                "providers": {
                    "glm": {"api_key": "k", "base_url": "https://glm",
                            "protocol": "anthropic", "models": {"caption": "glm-5v-turbo"}},
                    "ollama": {"api_key": "ollama-local", "base_url": "http://127.0.0.1:11434",
                               "protocol": "openai", "models": {"caption": "qwen3-vl:8b-instruct"}},
                },
            })
            result = self._load_with_home(tmp)
            self.assertEqual(result["provider"], "glm")
            self.assertIsNotNone(result["fallback"])
            self.assertEqual(result["fallback"]["provider"], "ollama")
            self.assertEqual(result["fallback"]["model"], "qwen3-vl:8b-instruct")
            self.assertEqual(result["fallback"]["protocol"], "openai")

    def test_fallback_name_not_found_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._write_config(tmp, {
                "caption_provider": "glm",
                "caption_fallback_provider": "typo-name",
                "providers": {"glm": {"api_key": "k", "base_url": "https://glm",
                                       "protocol": "anthropic",
                                       "models": {"caption": "glm-5v-turbo"}}},
            })
            result = self._load_with_home(tmp)
            self.assertIsNone(result["fallback"])


class TestProviderBundles(unittest.TestCase):
    def test_primary_only_when_no_fallback_configured(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            bundles = cap._stage_1_3_provider_bundles(cfg)
            self.assertEqual([label for label, _ in bundles], ["primary"])

    def test_both_bundles_when_fallback_configured(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(
                Path(d),
                caption_fallback_base_url="http://127.0.0.1:11434",
                caption_fallback_model="qwen3-vl:8b-instruct",
                caption_fallback_protocol="openai",
                caption_fallback_api_key="ollama-local",
            )
            bundles = cap._stage_1_3_provider_bundles(cfg)
            self.assertEqual([label for label, _ in bundles], ["primary", "fallback"])
            self.assertEqual(bundles[1][1]["model"], "qwen3-vl:8b-instruct")

    def test_fallback_requires_both_base_url_and_model(self):
        # A half-configured fallback (e.g. base_url set, model forgotten) must
        # not silently become an active bundle with an empty model field.
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d), caption_fallback_base_url="http://127.0.0.1:11434")
            bundles = cap._stage_1_3_provider_bundles(cfg)
            self.assertEqual(len(bundles), 1)


class TestCaptionOneImageWithFailover(unittest.TestCase):
    def setUp(self):
        self._orig = cap._stage_1_3_caption_one_image

    def tearDown(self):
        cap._stage_1_3_caption_one_image = self._orig

    def test_primary_success_never_tries_fallback(self):
        calls = []

        def fake(img, provider, media_dir, ctx_map):
            calls.append(provider["model"])
            return "a real caption", None
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(
                Path(d),
                caption_fallback_base_url="http://127.0.0.1:11434",
                caption_fallback_model="qwen3-vl:8b-instruct",
            )
            caption, err, used = cap._stage_1_3_caption_one_image_with_failover(
                {"filename": "p1.jpg"}, cfg, Path(d), {})
        self.assertEqual(caption, "a real caption")
        self.assertIsNone(err)
        self.assertEqual(used, "primary")
        self.assertEqual(calls, ["glm-5v-turbo"])  # fallback never invoked

    def test_primary_fails_fallback_succeeds(self):
        calls = []

        def fake(img, provider, media_dir, ctx_map):
            calls.append(provider["model"])
            if provider["model"] == "glm-5v-turbo":
                return None, "HTTPError: 429"
            return "local caption", None
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(
                Path(d),
                caption_fallback_base_url="http://127.0.0.1:11434",
                caption_fallback_model="qwen3-vl:8b-instruct",
            )
            caption, err, used = cap._stage_1_3_caption_one_image_with_failover(
                {"filename": "p1.jpg"}, cfg, Path(d), {})
        self.assertEqual(caption, "local caption")
        self.assertIsNone(err)
        self.assertEqual(used, "fallback")
        self.assertEqual(calls, ["glm-5v-turbo", "qwen3-vl:8b-instruct"])

    def test_both_fail_reports_fallback_error(self):
        def fake(img, provider, media_dir, ctx_map):
            return None, f"{provider['model']} down"
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(
                Path(d),
                caption_fallback_base_url="http://127.0.0.1:11434",
                caption_fallback_model="qwen3-vl:8b-instruct",
            )
            caption, err, used = cap._stage_1_3_caption_one_image_with_failover(
                {"filename": "p1.jpg"}, cfg, Path(d), {})
        self.assertIsNone(caption)
        self.assertEqual(err, "qwen3-vl:8b-instruct down")
        self.assertEqual(used, "fallback")

    def test_no_fallback_configured_reports_primary_error(self):
        def fake(img, provider, media_dir, ctx_map):
            return None, "primary down"
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))  # no fallback fields set
            caption, err, used = cap._stage_1_3_caption_one_image_with_failover(
                {"filename": "p1.jpg"}, cfg, Path(d), {})
        self.assertIsNone(caption)
        self.assertEqual(err, "primary down")
        self.assertEqual(used, "primary")


class TestFallbackSerialization(unittest.TestCase):
    """The fallback (local) provider gets exactly one concurrent call,
    independent of CAPTION_MAX_WORKERS — see _FALLBACK_SEMAPHORE."""

    def setUp(self):
        self._orig = cap._stage_1_3_caption_one_image

    def tearDown(self):
        cap._stage_1_3_caption_one_image = self._orig
        # Defensive: a failed assertion mid-test must not leave the process-wide
        # semaphore permanently held and starve every later test in this module.
        while cap._FALLBACK_SEMAPHORE.acquire(blocking=False):
            pass
        cap._FALLBACK_SEMAPHORE.release()

    def test_fallback_calls_never_overlap(self):
        active = {"count": 0, "max": 0}
        lock = threading.Lock()

        def fake(img, provider, media_dir, ctx_map):
            if provider["model"] != "glm-5v-turbo":  # the fallback bundle
                with lock:
                    active["count"] += 1
                    active["max"] = max(active["max"], active["count"])
                time.sleep(0.05)
                with lock:
                    active["count"] -= 1
                return "local caption", None
            return None, "primary always fails, forcing every image to fall over"
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(
                Path(d),
                caption_fallback_base_url="http://127.0.0.1:11434",
                caption_fallback_model="qwen3-vl:8b-instruct",
            )
            results = []
            results_lock = threading.Lock()

            def worker(i):
                r = cap._stage_1_3_caption_one_image_with_failover(
                    {"filename": f"p{i}.jpg"}, cfg, Path(d), {})
                with results_lock:
                    results.append(r)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(active["max"], 1,
                          "fallback provider received overlapping concurrent calls")
        self.assertEqual(len(results), 4)
        self.assertTrue(all(caption == "local caption" for caption, _err, _used in results))

    def test_primary_calls_still_run_concurrently(self):
        # Sanity check that the fallback semaphore does NOT also throttle the
        # primary — only non-primary bundles are gated.
        active = {"count": 0, "max": 0}
        lock = threading.Lock()

        def fake(img, provider, media_dir, ctx_map):
            with lock:
                active["count"] += 1
                active["max"] = max(active["max"], active["count"])
            time.sleep(0.05)
            with lock:
                active["count"] -= 1
            return "cloud caption", None
        cap._stage_1_3_caption_one_image = fake

        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))  # no fallback configured
            threads = [
                threading.Thread(
                    target=lambda i=i: cap._stage_1_3_caption_one_image_with_failover(
                        {"filename": f"p{i}.jpg"}, cfg, Path(d), {}))
                for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertGreater(active["max"], 1, "primary calls should overlap")


if __name__ == "__main__":
    unittest.main()
