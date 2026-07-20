#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_original_visual_tier import AnalysisError, build_report, load_benchmark


def benchmark(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "glic-original-realtime-cpu-benchmark-v1",
        "backend": "cpu-reference",
        "width": 960,
        "height": 540,
        "warmup_frames": 10,
        "frames": 120,
        "required_fps": 30.0,
        "certification_evidence_passed": True,
        "results": results,
    }


def result(name: str, mean_ms: float, p95_ms: float, passed: bool) -> dict[str, object]:
    return {
        "preset": name,
        "mean_ms": mean_ms,
        "p95_ms": p95_ms,
        "fps": 1000.0 / mean_ms,
        "process_passed": passed,
        "timing_passed": passed,
        "performance_passed": passed,
    }


class OriginalVisualTierAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previews = self.root / "previews"
        self.previews.mkdir()

        x = np.linspace(0, 255, 960, dtype=np.uint8)
        y = np.linspace(0, 255, 540, dtype=np.uint8)[:, None]
        dry = np.dstack(
            (
                np.broadcast_to(x, (540, 960)),
                np.broadcast_to(y, (540, 960)),
                np.broadcast_to((x[None, :] // 2 + y // 2), (540, 960)),
            )
        )
        self.dry = self.root / "dry.png"
        cv2.imwrite(str(self.dry), dry)
        cv2.imwrite(str(self.previews / "vv02.png"), 255 - dry)
        cv2.imwrite(str(self.previews / "webp.png"), np.roll(dry, 200, axis=1))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_benchmark(self, name: str, rows: list[dict[str, object]]) -> Path:
        path = self.root / name
        path.write_text(json.dumps(benchmark(rows)), encoding="utf-8")
        return path

    def test_repeat_consensus_is_intersection_and_visuals_are_analyzed(self) -> None:
        run1 = self.write_benchmark(
            "run1.json",
            [result("vv02", 20.0, 22.0, True), result("webp", 20.0, 22.0, True)],
        )
        run2 = self.write_benchmark(
            "run2.json",
            [result("vv02", 21.0, 24.0, True), result("webp", 40.0, 42.0, False)],
        )
        report = build_report(
            [run1, run2], self.dry, self.previews, 4, validate_full_corpus=False
        )
        self.assertEqual(report["counts"]["supported"], 2)
        self.assertEqual(report["counts"]["repeat_consensus_passed"], 1)
        self.assertEqual(report["performance_failures"], ["webp"])
        self.assertFalse(report["publishable"])
        self.assertFalse(report["counts"]["shortlist_quota_satisfied"])
        self.assertEqual(report["counts"]["shortlist_selected"], 1)
        self.assertEqual(report["diversity_shortlist"][0]["preset"], "vv02")
        self.assertTrue(report["rows"][0]["metrics"]["mae_rgb"] > 10.0)

    def test_reported_pass_cannot_override_frame_budget(self) -> None:
        lied = result("vv02", 40.0, 41.0, True)
        run = self.write_benchmark("lied.json", [lied])
        report = build_report(
            [run], self.dry, self.previews, 1, validate_full_corpus=False
        )
        self.assertEqual(report["counts"]["repeat_consensus_passed"], 0)
        reasons = report["rows"][0]["performance_runs"][0]["reasons"]
        self.assertIn("mean_exceeds_frame_budget", reasons)
        self.assertIn("p95_exceeds_frame_budget", reasons)

    def test_mismatched_name_sets_fail_closed(self) -> None:
        run1 = self.write_benchmark("a.json", [result("vv02", 20.0, 21.0, True)])
        run2 = self.write_benchmark("b.json", [result("webp", 20.0, 21.0, True)])
        with self.assertRaises(AnalysisError):
            build_report(
                [run1, run2],
                self.dry,
                self.previews,
                1,
                validate_full_corpus=False,
            )

    def test_duplicate_json_keys_fail_closed(self) -> None:
        path = self.root / "duplicate.json"
        path.write_text(
            '{"schema":"glic-original-realtime-cpu-benchmark-v1",'
            '"results":[],"results":[]}',
            encoding="utf-8",
        )
        with self.assertRaises(AnalysisError):
            load_benchmark(path)

    def test_zero_shortlist_quota_fails_closed(self) -> None:
        run = self.write_benchmark("run.json", [result("vv02", 20.0, 21.0, True)])
        with self.assertRaises(AnalysisError):
            build_report(
                [run],
                self.dry,
                self.previews,
                0,
                validate_full_corpus=False,
            )


if __name__ == "__main__":
    unittest.main()
