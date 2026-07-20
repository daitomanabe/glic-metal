#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_original_preset_catalog as catalog  # noqa: E402


def benchmark_row(
    preset: str,
    *,
    mean_ms: float = 5.0,
    p95_ms: float = 6.0,
    mapping_fidelity: str = "exact-compatible",
    mapping_reasons: list[str] | None = None,
) -> dict:
    return {
        "preset": preset,
        "backend": "metal",
        "median_ms": mean_ms,
        "p95_ms": p95_ms,
        "mean_ms": mean_ms,
        "median_gpu_ms": 1.0,
        "fps": 1000.0 / mean_ms,
        "process_passed": True,
        "performance_passed": True,
        "preset_mapping_fidelity": mapping_fidelity,
        "preset_mapping_reasons": mapping_reasons or [],
        "error": "",
    }


def benchmark_document(rows: list[dict]) -> dict:
    return {
        "schema": catalog.BENCHMARK_SCHEMA,
        "width": 960,
        "height": 540,
        "frames": 120,
        "warmup_frames": 10,
        "required_fps": 30.0,
        "strength": 1.0,
        "preset_semantics": "original",
        "processing_mode": "compat_realtime",
        "results": rows,
    }


def perceptual_fixture(value: float, hash_value: str = "0000000000000000") -> dict:
    return {
        "residual_luma_grid": [value] * 64,
        "residual_edge_grid": [value] * 64,
        "residual_scale_histogram": [value] * 7,
        "residual_orientation": [value] * 2,
        "residual_phash": hash_value,
        "hsv_hist": [value] * 128,
        "color_grid": [value] * 48,
        "phash": hash_value,
    }


def liveliness_row(name: str) -> dict:
    return {"name": name, **{field: float(index + 1) for index, field in enumerate(catalog.LIVELINESS_FIELDS)}}


class PerformanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = benchmark_document([])
        self.row = benchmark_row("default")

    def test_exact_frame_budget_passes(self) -> None:
        boundary = 1000.0 / 30.0
        self.row["mean_ms"] = boundary
        self.row["p95_ms"] = boundary
        self.assertEqual(catalog.performance_gate_reasons(self.row, self.root), [])

    def test_each_mandatory_condition_fails_closed(self) -> None:
        cases = {
            "benchmark_resolution_mismatch": ("root", "width", 959),
            "backend_is_not_metal": ("row", "backend", "cpu"),
            "insufficient_warmup_frames": ("root", "warmup_frames", 9),
            "insufficient_measured_frames": ("root", "frames", 119),
            "mean_slower_than_30fps": ("row", "mean_ms", 34.0),
            "p95_slower_than_30fps": ("row", "p95_ms", 34.0),
            "processing_did_not_pass": ("row", "process_passed", False),
            "missing_or_invalid_p95_ms": ("row", "p95_ms", None),
            "missing_or_invalid_mean_ms": ("row", "mean_ms", "5.0"),
        }
        for reason, (target, key, value) in cases.items():
            with self.subTest(reason=reason):
                root = json.loads(json.dumps(self.root))
                row = json.loads(json.dumps(self.row))
                (root if target == "root" else row)[key] = value
                self.assertIn(reason, catalog.performance_gate_reasons(row, root))

    def test_missing_record_is_not_imputed(self) -> None:
        self.assertEqual(
            catalog.performance_gate_reasons(None, self.root), ["missing_benchmark_record"]
        )


class MappingGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = benchmark_document([])

    def test_exact_mapping_is_original_compatible(self) -> None:
        gate = catalog.mapping_gate(benchmark_row("exact"), self.root)
        self.assertEqual(gate["tier"], "exact-compatible")
        self.assertTrue(gate["original_compatible"])

    def test_approximated_mapping_has_separate_tier(self) -> None:
        gate = catalog.mapping_gate(
            benchmark_row(
                "approx",
                mapping_fidelity="approximated",
                mapping_reasons=["wavelet_projection"],
            ),
            self.root,
        )
        self.assertEqual(gate["tier"], "approximated")
        self.assertFalse(gate["original_compatible"])
        self.assertTrue(gate["approximation_tier"])

    def test_unsupported_mapping_is_never_promoted(self) -> None:
        gate = catalog.mapping_gate(
            benchmark_row(
                "unsupported",
                mapping_fidelity="unsupported",
                mapping_reasons=["random_wavelet_requires_per_encode_selection"],
            ),
            self.root,
        )
        self.assertEqual(gate["tier"], "unsupported")
        self.assertFalse(gate["original_compatible"])
        self.assertFalse(gate["approximation_tier"])

    def test_exact_mapping_with_reasons_is_rejected(self) -> None:
        gate = catalog.mapping_gate(
            benchmark_row(
                "contradictory",
                mapping_fidelity="exact-compatible",
                mapping_reasons=["random_wavelet_requires_per_encode_selection"],
            ),
            self.root,
        )
        self.assertEqual(gate["tier"], "unsupported")
        self.assertIn(
            "exact_mapping_must_not_report_approximation_reasons", gate["reasons"]
        )

    def test_legacy_or_unreported_mapping_fails_closed(self) -> None:
        row = benchmark_row("legacy")
        del row["preset_mapping_fidelity"]
        root = benchmark_document([])
        root["preset_semantics"] = "legacy"
        gate = catalog.mapping_gate(row, root)
        self.assertEqual(gate["tier"], "unsupported")
        self.assertIn("preset_semantics_is_not_original", gate["reasons"])
        self.assertIn("missing_or_invalid_preset_mapping_fidelity", gate["reasons"])


class UpstreamProvenanceTests(unittest.TestCase):
    def test_repository_corpus_matches_pinned_upstream_manifest(self) -> None:
        evidence = catalog.verify_upstream_preset_corpus(
            catalog.DEFAULT_UPSTREAM_PRESET_MANIFEST,
            catalog.DEFAULT_UPSTREAM_PRESET_DIRECTORY,
        )
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["reasons"], [])
        self.assertEqual(evidence["upstream_commit"], catalog.UPSTREAM_PRESET_COMMIT)
        self.assertEqual(len(evidence["manifest_names"]), 144)


class ImageMetricTests(unittest.TestCase):
    def test_identical_image_has_zero_difference(self) -> None:
        image = np.full((20, 30, 3), 128, dtype=np.uint8)
        metrics = catalog.difference_metrics(image, image.copy())
        self.assertEqual(metrics["mae_rgb"], 0.0)
        self.assertEqual(metrics["changed_pixel_ratio"], 0.0)
        self.assertEqual(metrics["global_luma_ssim"], 1.0)
        self.assertEqual(metrics["windowed_luma_ssim"], 1.0)
        self.assertIn(
            "effect_mae_below_visible_floor", catalog.visual_gate_reasons(metrics)
        )

    def test_changed_image_is_detected(self) -> None:
        dry = np.zeros((20, 30, 3), dtype=np.uint8)
        wet = np.full_like(dry, 64)
        metrics = catalog.difference_metrics(dry, wet)
        self.assertEqual(metrics["mae_rgb"], 64.0)
        self.assertEqual(metrics["changed_pixel_ratio"], 1.0)
        self.assertEqual(metrics["strong_changed_pixel_ratio"], 1.0)
        self.assertEqual(metrics["highlight_clipping_ratio"], 0.0)
        self.assertEqual(catalog.visual_gate_reasons(metrics), [])

    def test_severe_highlight_clipping_is_rejected(self) -> None:
        dry = np.full((20, 30, 3), 64, dtype=np.uint8)
        wet = np.full_like(dry, 255)
        metrics = catalog.difference_metrics(dry, wet)
        self.assertEqual(metrics["highlight_clipping_ratio"], 1.0)
        self.assertIn("severe_highlight_clipping", catalog.visual_gate_reasons(metrics))

    def test_dimensions_must_match(self) -> None:
        with self.assertRaises(catalog.CatalogError):
            catalog.difference_metrics(
                np.zeros((10, 10, 3), dtype=np.uint8),
                np.zeros((11, 10, 3), dtype=np.uint8),
            )


class DiversityTests(unittest.TestCase):
    def test_farthest_pattern_precedes_near_duplicate(self) -> None:
        rows = [
            {
                "preset": "seed",
                "effect_presence_score": 0.9,
                "perceptual": perceptual_fixture(0.0),
            },
            {
                "preset": "near",
                "effect_presence_score": 0.8,
                "perceptual": perceptual_fixture(0.02),
            },
            {
                "preset": "far",
                "effect_presence_score": 0.7,
                "perceptual": perceptual_fixture(1.0, "ffffffffffffffff"),
            },
        ]
        ordered = catalog.greedy_diversity_order(rows)
        self.assertEqual([row["preset"] for row in ordered], ["seed", "far", "near"])
        self.assertGreater(
            ordered[1]["min_distance_to_earlier"], ordered[2]["min_distance_to_earlier"]
        )

    def test_distance_is_symmetric_and_bounded(self) -> None:
        left = perceptual_fixture(0.0)
        right = perceptual_fixture(1.0, "ffffffffffffffff")
        distance = catalog.visual_distance(left, right)
        self.assertEqual(distance, catalog.visual_distance(right, left))
        self.assertGreater(distance, 0.5)
        self.assertLessEqual(distance, 1.0)

    def test_selection_does_not_fill_with_near_duplicates(self) -> None:
        ordered = catalog.greedy_diversity_order(
            [
                {
                    "preset": "seed",
                    "effect_presence_score": 0.9,
                    "perceptual": perceptual_fixture(0.0),
                },
                {
                    "preset": "duplicate",
                    "effect_presence_score": 0.8,
                    "perceptual": perceptual_fixture(0.001),
                },
            ]
        )
        catalog.apply_diversity_selection(
            ordered, 2, rank_field="rank", selected_field="selected"
        )
        self.assertTrue(ordered[0]["selected"])
        self.assertFalse(ordered[1]["selected"])


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        yy, xx = np.indices((540, 960))
        base = np.stack(
            [
                (xx % 256).astype(np.uint8),
                (yy % 256).astype(np.uint8),
                ((xx + yy) % 256).astype(np.uint8),
            ],
            axis=2,
        )
        self.dry = self.root / "dry.png"
        cv2.imwrite(str(self.dry), base)
        self.previews = self.root / "previews"
        self.previews.mkdir()
        cv2.imwrite(str(self.previews / "alpha.png"), np.roll(base, 48, axis=1))
        checker = base.copy()
        checker[((xx // 32 + yy // 32) % 2) == 0] ^= 0x7F
        cv2.imwrite(str(self.previews / "beta.png"), checker)
        cv2.imwrite(str(self.previews / "slow.png"), 255 - base)
        self.benchmark = self.root / "benchmark.json"
        self.benchmark.write_text(
            json.dumps(
                benchmark_document(
                    [
                        benchmark_row("alpha"),
                        benchmark_row(
                            "beta",
                            mapping_fidelity="approximated",
                            mapping_reasons=["symlet11_to_symlet20_not_implemented"],
                        ),
                        benchmark_row("slow", p95_ms=34.0),
                    ]
                )
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_end_to_end_report_and_fidelity_labels(self) -> None:
        liveliness = self.root / "liveliness.json"
        liveliness.write_text(
            json.dumps([liveliness_row("alpha"), liveliness_row("beta")]), encoding="utf-8"
        )
        previews = catalog.discover_previews(self.previews)
        document = catalog.build_catalog(
            self.benchmark,
            previews,
            self.dry,
            expected_count=3,
            select_count=2,
            liveliness_path=liveliness,
            generated_at="2026-07-20T00:00:00Z",
        )
        self.assertEqual(document["status"], "ok")
        self.assertFalse(document["publishable"])
        self.assertFalse(document["summary"]["upstream_provenance_verified"])
        self.assertIn(
            "upstream_preset_corpus_evidence_not_supplied",
            document["publication_blockers"],
        )
        self.assertEqual(document["summary"]["performance_passed"], 2)
        self.assertEqual(document["summary"]["visual_integrity_passed"], 3)
        self.assertEqual(document["summary"]["eligible"], 1)
        self.assertEqual(document["summary"]["selected"], 1)
        self.assertEqual(document["summary"]["mapping_exact_compatible"], 2)
        self.assertEqual(document["summary"]["mapping_approximated"], 1)
        self.assertEqual(document["summary"]["mapping_unsupported"], 0)
        self.assertEqual(document["summary"]["approximated_eligible"], 1)
        self.assertEqual(document["summary"]["approximated_selected"], 1)
        self.assertEqual(document["summary"]["liveliness_valid"], 2)
        self.assertEqual(document["summary"]["liveliness_missing_or_invalid"], 1)
        self.assertEqual(
            document["policy"]["metric_families"]["aesthetic_quality"], "not_scored"
        )
        self.assertEqual(document["fidelity"]["codec_pixel_fidelity"], "not_claimed")
        self.assertEqual(
            document["fidelity"]["preset_parameters"], "per_row_mapping_fidelity_required"
        )
        self.assertEqual(
            document["fidelity"]["render_path"], "glic_metal_realtime_visual_approximation"
        )
        slow = next(row for row in document["results"] if row["preset"] == "slow")
        self.assertFalse(slow["eligible"])
        self.assertIn("p95_slower_than_30fps", slow["performance_gate"]["reasons"])
        beta = next(row for row in document["results"] if row["preset"] == "beta")
        self.assertFalse(beta["eligible"])
        self.assertTrue(beta["approximation_eligible"])
        self.assertEqual(beta["preset_mapping"]["tier"], "approximated")

        output = self.root / "output"
        catalog.write_json(output / "ranking.json", document)
        catalog.write_csv(output / "ranking.csv", document)
        catalog.write_html(output / "index.html", document)
        loaded = json.loads((output / "ranking.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema"], catalog.SCHEMA)
        with (output / "ranking.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertTrue(
            all(row["render_path_fidelity"] == "glic_metal_realtime_visual_approximation" for row in rows)
        )
        page = (output / "index.html").read_text(encoding="utf-8")
        self.assertIn("Fidelity boundary", page)
        self.assertIn("not a pixel-fidelity", page)

    def test_missing_preview_makes_corpus_incomplete_and_rejects_row(self) -> None:
        (self.previews / "beta.png").unlink()
        previews = catalog.discover_previews(self.previews)
        document = catalog.build_catalog(
            self.benchmark,
            previews,
            self.dry,
            expected_count=3,
            select_count=2,
        )
        self.assertEqual(document["status"], "incomplete")
        self.assertFalse(document["publishable"])
        beta = next(row for row in document["results"] if row["preset"] == "beta")
        self.assertIn("missing_preview", beta["preview_gate"]["reasons"])

    def test_compact_manifest_resolves_paths_relative_to_manifest(self) -> None:
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"alpha": "previews/alpha.png"}), encoding="utf-8")
        loaded = catalog.load_preview_manifest(manifest)
        self.assertEqual(loaded["alpha"], (self.previews / "alpha.png").resolve())

    def test_invalid_liveliness_is_attached_but_not_used_as_aesthetic_gate(self) -> None:
        liveliness = self.root / "bad-liveliness.json"
        row = liveliness_row("alpha")
        del row["shape_entropy"]
        liveliness.write_text(json.dumps([row]), encoding="utf-8")
        document = catalog.build_catalog(
            self.benchmark,
            catalog.discover_previews(self.previews),
            self.dry,
            expected_count=3,
            select_count=2,
            liveliness_path=liveliness,
        )
        alpha = next(item for item in document["results"] if item["preset"] == "alpha")
        self.assertFalse(alpha["liveliness_gate"]["passed"])
        self.assertTrue(alpha["eligible"])
        self.assertIsNone(alpha["liveliness"])


if __name__ == "__main__":
    unittest.main()
