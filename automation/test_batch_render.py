import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

if "huggingface_hub" not in sys.modules:
    hf_mod = types.ModuleType("huggingface_hub")
    hf_err = types.ModuleType("huggingface_hub.errors")

    class _HfHubHTTPError(Exception):
        def __init__(self, message: str = "", response: Any = None) -> None:
            super().__init__(message)
            self.response = response

    hf_mod.HfApi = MagicMock
    hf_mod.CommitOperationAdd = MagicMock
    hf_mod.hf_hub_download = MagicMock()
    hf_err.HfHubHTTPError = _HfHubHTTPError
    sys.modules["huggingface_hub"] = hf_mod
    sys.modules["huggingface_hub.errors"] = hf_err

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_render as br  # noqa: E402
from huggingface_hub.errors import HfHubHTTPError  # noqa: E402


class BatchRenderLogicTests(unittest.TestCase):
    def test_pending_and_failure_priority(self) -> None:
        sources = {
            f"data/piece_{i:04d}.mid": 10000 for i in range(1517)
        }
        sources["README.md"] = 512
        targets = {
            br.target_mp4_path("data/piece_0000.mid"): 500_000,
            br.target_mp4_path("data/piece_0001.mid"): 128,  # corrupted/truncated -> must re-render
        }
        prev_failures = {
            "data/piece_0005.mid": {"attempts": 2, "last_error": "ffmpeg crash"},
        }
        pending, completed = br.compute_pending_items(sources, targets, prev_failures)
        self.assertEqual(completed, ["data/piece_0000.mid"])
        self.assertEqual(len(pending), 1516)
        self.assertEqual(pending[0]["source_path"], "data/piece_0005.mid")
        self.assertEqual(pending[0]["attempts"], 2)

    def test_sharding_partitions_cleanly_without_overlap(self) -> None:
        items = [{"source_path": f"p_{i}.mid", "target_path": f"videos/p_{i}.mp4"} for i in range(1517)]
        seen: list[str] = []
        for idx in range(20):
            s = br.shard_items(items, idx, 20)
            self.assertGreaterEqual(len(s), 75)
            self.assertLessEqual(len(s), 76)
            seen.extend(x["source_path"] for x in s)
        self.assertEqual(len(seen), 1517)
        self.assertEqual(len(set(seen)), 1517)

    def test_private_to_public_fallback_when_private_forbidden(self) -> None:
        api = MagicMock()
        api.whoami.return_value = {"name": "RdxradiTor"}
        resp = MagicMock()
        resp.status_code = 403
        api.repo_info.side_effect = HfHubHTTPError("Forbidden", response=resp)
        api.create_repo.side_effect = [Exception("Private repos restricted"), None]
        repo, used_fallback = br.resolve_target_dataset(api, "RdxradiTor/sus")
        self.assertTrue(used_fallback)
        self.assertEqual(repo, "RdxradiTor/sus-public")


if __name__ == "__main__":
    unittest.main()
