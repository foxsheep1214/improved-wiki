"""Cross-process caption limiting and per-round worker-cap regressions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_1_3_caption as caption  # noqa: E402


class CaptionProcessLimitTests(unittest.TestCase):
    def test_one_round_honors_explicit_max_workers(self):
        pending = [{"filename": f"p{i}.jpg"} for i in range(5)]
        config = SimpleNamespace()
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(caption, "ThreadPoolExecutor") as pool,
            patch.object(caption, "as_completed", return_value=[]),
        ):
            executor = pool.return_value.__enter__.return_value
            executor.submit.side_effect = lambda *_args, **_kwargs: object()
            result = caption._stage_1_3_caption_one_round(
                pending, config, Path(d), {}, "", max_workers=2)
        self.assertEqual(result, 0)
        pool.assert_called_once_with(max_workers=2)

    def test_caption_batch_enters_global_slot(self):
        image = {"filename": "p1.jpg"}
        config = SimpleNamespace(caption_api_key="key")
        entered: list[bool] = []

        @contextmanager
        def fake_slot():
            entered.append(True)
            yield

        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(
                caption, "_stage_1_3_pending_images",
                side_effect=[[image], []],
            ),
            patch.object(caption, "_stage_1_3_build_context_map", return_value={}),
            patch.object(
                caption, "_stage_1_3_caption_one_round", return_value=1,
            ) as one_round,
            patch.object(caption, "_caption_batch_slot", fake_slot),
            patch.object(caption, "update_worker_phase"),
        ):
            result = caption._stage_1_3_caption_images_batch(
                [image], config, Path(d), max_workers=3)

        self.assertEqual(result, 1)
        self.assertEqual(entered, [True])
        self.assertEqual(one_round.call_args.kwargs["max_workers"], 3)


if __name__ == "__main__":
    unittest.main()
