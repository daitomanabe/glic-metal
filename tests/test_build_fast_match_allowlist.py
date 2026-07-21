#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_fast_match_allowlist.py"


class FastMatchAllowlistTest(unittest.TestCase):
    def test_allows_matching_preview_and_rejects_visual_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            strict_dir = root / "strict"
            fast_dir = root / "fast"
            strict_dir.mkdir()
            fast_dir.mkdir()
            y, x = np.mgrid[0:96, 0:128]
            image = np.stack(
                ((x * 2) % 256, (y * 3) % 256, (x + y) % 256), axis=2
            ).astype(np.uint8)
            cv2.imwrite(str(strict_dir / "matching.png"), image)
            cv2.imwrite(str(fast_dir / "matching.png"), image)
            cv2.imwrite(str(strict_dir / "different.png"), image)
            cv2.imwrite(str(fast_dir / "different.png"), 255 - image)

            strict_json = root / "strict.json"
            fast_json = root / "fast.json"
            common_rows = [
                {
                    "preset": preset,
                    "preset_config_fnv1a64": f"{index + 1:016x}",
                    "mean_ms": 30.0,
                    "p95_ms": 31.0,
                    "fps": 33.3,
                }
                for index, preset in enumerate(("matching", "different"))
            ]
            strict_json.write_text(
                json.dumps(
                    {
                        "fidelity_mode": "strict",
                        "input_decoded_color_fnv1a64": "0000000000000001",
                        "results": common_rows,
                    }
                ),
                encoding="utf-8",
            )
            fast_rows = [dict(row, mean_ms=20.0, p95_ms=22.0, fps=50.0) for row in common_rows]
            fast_json.write_text(
                json.dumps(
                    {
                        "fidelity_mode": "fast-match",
                        "input_decoded_color_fnv1a64": "0000000000000001",
                        "results": fast_rows,
                    }
                ),
                encoding="utf-8",
            )
            output = root / "allowlist.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--case",
                    "fixture",
                    str(strict_json),
                    str(fast_json),
                    str(strict_dir),
                    str(fast_dir),
                    "--output-json",
                    str(output),
                ],
                check=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["allowlist"], ["matching"])
            rows = {row["preset"]: row for row in payload["results"]}
            self.assertTrue(rows["matching"]["allowed"])
            self.assertFalse(rows["different"]["allowed"])
            self.assertIn("windowed_luma_ssim", rows["different"]["reasons"])


if __name__ == "__main__":
    unittest.main()
