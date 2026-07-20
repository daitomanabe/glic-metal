#!/usr/bin/env python3

from __future__ import annotations

import copy
import csv
import random
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rank_search_results as ranking  # noqa: E402
import analyze_search_images as image_analysis  # noqa: E402


def valid_metrics() -> dict[str, float]:
    return {
        "mae": 42.0,
        "changed_ratio": 0.72,
        "luma_correlation": 0.70,
        "structure": 0.62,
        "clipping_ratio": 0.12,
        "entropy": 0.90,
        "temporal_residual_delta": 0.12,
        "content_dependency": 0.80,
        "output_stddev": 0.22,
        "min_input_changed_ratio": 0.62,
        "mean_process_ms": 1.0,
    }


def descriptor(index: int, length: int, multiplier: int) -> list[float]:
    return [((index * multiplier + position * 17) % 101) / 100.0 for position in range(length)]


def candidate_recipe(index: int) -> dict:
    return {
        "color_space": index % 16,
        "strength": (index % 100) / 100.0,
        "border_rgb": [index % 256, index * 3 % 256, index * 7 % 256],
        "channels": [
            {
                "min_block": 2 + index % 30,
                "max_block": 2 + index % 30,
                "segmentation_precision": index % 100,
                "prediction": index % 24,
                "quantization": index % 256,
                "clamp": index % 2,
                "transform": (index // 2) % 2,
                "wavelet": index % 24,
                "transform_compress": index % 200,
                "transform_scale": index % 100,
                "encoding": index % 6,
            }
        ],
    }


def valid_certification(
    candidate_id: str = "1", recipe_hash: str = "0000000000000001", recipe: dict | None = None
) -> dict:
    recipe = recipe or candidate_recipe(1)
    p95_ms = 5.0
    return {
        "candidate_id": candidate_id,
        "recipe_hash": recipe_hash,
        "recipe_sha256": ranking.canonical_recipe_sha256(recipe),
        "status": "passed",
        "backend": "metal",
        "width": 960,
        "height": 540,
        "warmup_frames": 10,
        "frames_measured": 120,
        "mean_ms": 4.0,
        "median_ms": 4.0,
        "p95_ms": p95_ms,
        "p99_ms": 5.5,
        "max_ms": 6.0,
        "p95_fps": 1000.0 / p95_ms,
        "process_passed": True,
        "performance_passed": True,
        "error": "",
    }


def valid_certification_payload(candidates: list[dict], archive_sha: str = "a" * 64) -> dict:
    records = {}
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        recipe_hash = str(candidate["recipe_hash"])
        recipe = candidate["record"]["recipe"]
        records[recipe_hash] = valid_certification(candidate_id, recipe_hash, recipe)
    return {
        "schema": ranking.PERFORMANCE_CERTIFICATION_SCHEMA,
        "generated_at": "2026-07-20T00:00:00Z",
        "source": {
            "archive_sha256": archive_sha,
            "input_sha256": "b" * 64,
            "binary_sha256": "c" * 64,
            "metallib_sha256": "d" * 64,
            "hardware": {"fingerprint": "e" * 64},
        },
        "policy": {
            "version": ranking.PERFORMANCE_POLICY_VERSION,
            "backend": "metal",
            "width": 960,
            "height": 540,
            "warmup_frames": 10,
            "measured_frames": 120,
            "required_fps": 30.0,
            "max_p95_ms": 1000.0 / 30.0,
            "recipe_sha256_method": ranking.RECIPE_SHA256_METHOD,
        },
        "records": records,
    }


def ranking_item(index: int) -> dict:
    metric = valid_metrics()
    metric.update(
        {
            "mae": 25.0 + (index % 31),
            "changed_ratio": 0.45 + (index % 18) / 50.0,
            "luma_correlation": 0.35 + (index % 14) / 25.0,
            "structure": 0.30 + (index % 16) / 25.0,
            "clipping_ratio": 0.07 + (index % 12) / 100.0,
            "entropy": 0.72 + (index % 19) / 70.0,
            "temporal_residual_delta": 0.07 + (index % 14) / 100.0,
            "content_dependency": 0.45 + (index % 16) / 22.0,
            "output_stddev": 0.12 + (index % 14) / 80.0,
            "min_input_changed_ratio": 0.35 + (index % 18) / 40.0,
            "mean_process_ms": 0.5 + (index % 7) / 10.0,
        }
    )
    live = {
        "lum_mean": 90.0 + index % 50,
        "occ_soft": 0.55 + (index % 35) / 100.0,
        "occ_hard": 0.35 + (index % 40) / 100.0,
        "bg": float(index % 45),
        "blobs": float(2 + index % 31),
        "area_cv": 0.5 + (index % 28) / 6.0,
        "elong_mean": 1.2 + (index % 20) / 5.0,
        "elong_cv": (index % 12) / 10.0,
        "solidity": 0.60 + (index % 30) / 100.0,
        "shape_entropy": 0.15 + (index % 28) / 35.0,
    }
    visual = {
        "colorfulness": 0.10 + (index % 25) / 30.0,
        "saturation_mean": 0.15 + (index % 30) / 40.0,
        "saturation_std": 0.10 + (index % 22) / 35.0,
        "local_contrast": 0.03 + (index % 18) / 50.0,
        "edge_density": 0.03 + (index % 24) / 40.0,
        "blockiness": 0.01 + (index % 20) / 45.0,
        "channel_separation": 0.05 + (index % 27) / 35.0,
        "phash": f"{(index * 0x9E3779B97F4A7C15) & ((1 << 64) - 1):016x}",
        "dhash": f"{(index * 0xD1B54A32D192ED03) & ((1 << 64) - 1):016x}",
        "hsv_hist": [1.0 if position == index % 128 else 0.0 for position in range(128)],
        "luma_grid": descriptor(index, 64, 23),
        "edge_grid": descriptor(index, 64, 47),
        "color_grid": descriptor(index, 48, 71),
    }
    consistency = min(1.0, metric["min_input_changed_ratio"] / metric["changed_ratio"])
    recipe = candidate_recipe(index)
    recipe_hash = f"{index:016x}"
    certification = valid_certification(str(index), recipe_hash, recipe)
    certification["certified"] = True
    certification["gate_reasons"] = []
    return {
        "candidate_id": str(index),
        "recipe_hash": recipe_hash,
        "preview_hash": f"{index + 1000:016x}",
        "evaluation_hash": f"{index + 2000:016x}",
        "archive_cell": f"cell-{index}",
        "generation": "mutation",
        "parent_hash": "",
        "recipe_family": f"family-{index}",
        "color_space": index % 16,
        "eligible": True,
        "hard_gate": {"passed": True, "reasons": []},
        "performance_certification": certification,
        "raw_metrics": metric,
        "liveliness": live,
        "perceptual": visual,
        "derived_metrics": {
            "mean_fps": 1000.0 / metric["mean_process_ms"],
            "search_mean_fps": 1000.0 / metric["mean_process_ms"],
            "certified_p95_fps": certification["p95_fps"],
            "certified_headroom_ratio": certification["p95_fps"] / 30.0,
            "input_consistency": consistency,
            "abs_luma_correlation": abs(metric["luma_correlation"]),
            "unclipped": 1.0 - metric["clipping_ratio"],
        },
        "search_quality": 0.5 + index / 1000.0,
        "preview_path": f"elites/{index:016x}.png",
        "recipe": recipe,
    }


class HardGateTests(unittest.TestCase):
    def test_exact_boundaries_pass(self) -> None:
        metric = valid_metrics()
        metric.update(
            {
                "mae": 8.0,
                "changed_ratio": 0.20,
                "min_input_changed_ratio": 0.15,
                "entropy": 0.12,
                "output_stddev": 0.031,
                "clipping_ratio": 0.25,
                "luma_correlation": -0.10,
                "structure": 0.15,
                "content_dependency": 0.15,
                "mean_process_ms": 1000.0 / 30.0,
            }
        )
        self.assertEqual(ranking.hard_gate_reasons(metric), [])

    def test_each_gate_rejects(self) -> None:
        cases = {
            "below_30_fps_lowres_prefilter": ("mean_process_ms", 34.0),
            "no_op": ("mae", 7.99),
            "excessive_change": ("changed_ratio", 0.951),
            "collapsed_output": ("entropy", 0.119),
            "excessive_clipping": ("clipping_ratio", 0.251),
            "input_independent_noise": ("structure", 0.149),
        }
        for reason, (field, value) in cases.items():
            with self.subTest(reason=reason):
                metric = valid_metrics()
                metric[field] = value
                self.assertIn(reason, ranking.hard_gate_reasons(metric))

    def test_missing_is_not_imputed(self) -> None:
        metric = valid_metrics()
        del metric["structure"]
        self.assertEqual(ranking.hard_gate_reasons(metric), ["missing_or_nonfinite:structure"])

    def test_zero_process_time_is_rejected(self) -> None:
        metric = valid_metrics()
        metric["mean_process_ms"] = 0.0
        self.assertIn("invalid_process_time", ranking.hard_gate_reasons(metric))


class PerformanceCertificationGateTests(unittest.TestCase):
    def test_exact_30fps_boundary_passes(self) -> None:
        certification = valid_certification()
        boundary = 1000.0 / 30.0
        certification.update(
            {"mean_ms": boundary, "p95_ms": boundary, "p95_fps": 30.0}
        )
        self.assertEqual(ranking.performance_gate_reasons(certification), [])

    def test_missing_certification_fails_closed(self) -> None:
        self.assertEqual(
            ranking.performance_gate_reasons(None),
            ["missing_960x540_performance_certification"],
        )

    def test_wrong_backend_is_rejected(self) -> None:
        certification = valid_certification()
        certification["backend"] = "cpu"
        self.assertIn(
            "performance_backend_mismatch",
            ranking.performance_gate_reasons(certification),
        )

    def test_wrong_resolution_is_rejected(self) -> None:
        certification = valid_certification()
        certification["width"] = 959
        self.assertIn(
            "performance_resolution_mismatch",
            ranking.performance_gate_reasons(certification),
        )

    def test_insufficient_frames_are_rejected(self) -> None:
        certification = valid_certification()
        certification["frames_measured"] = 119
        self.assertIn(
            "insufficient_performance_measured_frames",
            ranking.performance_gate_reasons(certification),
        )

    def test_p95_over_budget_is_rejected(self) -> None:
        certification = valid_certification()
        certification["p95_ms"] = 1000.0 / 30.0 + 1e-6
        certification["p95_fps"] = 1000.0 / certification["p95_ms"]
        self.assertIn(
            "below_30_fps_p95_at_960x540",
            ranking.performance_gate_reasons(certification),
        )

    def test_certified_p95_fps_drives_headroom_score(self) -> None:
        item = ranking_item(1)
        calibration = ranking.raw_calibration([item])
        item["derived_metrics"]["mean_fps"] = 100000.0
        item["derived_metrics"]["search_mean_fps"] = 100000.0
        item["derived_metrics"]["certified_p95_fps"] = 30.0
        slow = ranking.score_families(item, calibration)["realtime_headroom"]
        item["derived_metrics"]["certified_p95_fps"] = 60.0
        fast = ranking.score_families(item, calibration)["realtime_headroom"]
        self.assertEqual(slow, 0.0)
        self.assertEqual(fast, 1.0)


class PerformanceCertificationPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive_sha = "a" * 64
        self.candidate = {
            "candidate_id": "1",
            "recipe_hash": "0000000000000001",
            "record": {"recipe": candidate_recipe(1)},
        }

    def test_complete_snapshot_validates(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        indexed = ranking.validate_performance_certifications(
            payload, [self.candidate], self.archive_sha
        )
        self.assertEqual(set(indexed), {self.candidate["recipe_hash"]})

    def test_numeric_zero_candidate_id_is_preserved(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["candidate_id"] = "0"
        payload = valid_certification_payload([candidate], self.archive_sha)
        payload["records"][candidate["recipe_hash"]]["candidate_id"] = 0
        indexed = ranking.validate_performance_certifications(
            payload, [candidate], self.archive_sha
        )
        self.assertEqual(indexed[candidate["recipe_hash"]]["candidate_id"], 0)

    def test_missing_policy_version_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        del payload["policy"]["version"]
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_missing_recipe_identity_method_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        del payload["policy"]["recipe_sha256_method"]
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_archive_mismatch_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], "b" * 64)
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_missing_metallib_identity_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        del payload["source"]["metallib_sha256"]
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_missing_row_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        payload["records"] = {}
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_extra_row_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        extra = valid_certification("2", "extra", candidate_recipe(2))
        payload["records"]["extra"] = extra
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_recipe_sha_mismatch_is_rejected(self) -> None:
        payload = valid_certification_payload([self.candidate], self.archive_sha)
        payload["records"][self.candidate["recipe_hash"]]["recipe_sha256"] = "0" * 64
        with self.assertRaises(ranking.CertificationValidationError):
            ranking.validate_performance_certifications(
                payload, [self.candidate], self.archive_sha
            )

    def test_recipe_sha_is_order_independent(self) -> None:
        recipe = candidate_recipe(1)
        reversed_recipe = dict(reversed(list(recipe.items())))
        self.assertEqual(
            ranking.canonical_recipe_sha256(recipe),
            ranking.canonical_recipe_sha256(reversed_recipe),
        )


class ParetoTests(unittest.TestCase):
    def test_dominance_and_tradeoff(self) -> None:
        rows = [
            {"recipe_hash": "a", "candidate_id": "a", "pareto_vector": {"x": 1.0, "y": 1.0}},
            {"recipe_hash": "b", "candidate_id": "b", "pareto_vector": {"x": 0.5, "y": 0.5}},
            {"recipe_hash": "c", "candidate_id": "c", "pareto_vector": {"x": 1.0, "y": 0.0}},
            {"recipe_hash": "d", "candidate_id": "d", "pareto_vector": {"x": 0.0, "y": 1.0}},
        ]
        fronts = ranking.nondominated_fronts(rows, ("x", "y"))
        self.assertEqual([row["recipe_hash"] for row in fronts[0]], ["a"])
        self.assertEqual({row["recipe_hash"] for row in fronts[1]}, {"b", "c", "d"})

    def test_quantization_suppresses_small_noise(self) -> None:
        self.assertEqual(ranking.quantize(0.611), ranking.quantize(0.619))

    def test_constant_crowding_axis_has_no_arbitrary_endpoints(self) -> None:
        rows = [
            {"recipe_hash": name, "family_scores": {"x": 0.5}}
            for name in ("a", "b", "c")
        ]
        ranking.assign_crowding(rows, ("x",))
        self.assertEqual([row["crowding_distance"] for row in rows], [0.0, 0.0, 0.0])


class PerceptualTests(unittest.TestCase):
    def test_identical_descriptor_is_duplicate(self) -> None:
        left = ranking_item(1)
        right = copy.deepcopy(left)
        right["recipe_hash"] = "different"
        right["preview_hash"] = "different"
        self.assertTrue(ranking.is_near_duplicate(left, right))

    def test_distinct_descriptor_is_not_duplicate(self) -> None:
        self.assertFalse(ranking.is_near_duplicate(ranking_item(1), ranking_item(37)))


class RankingTests(unittest.TestCase):
    def test_deterministic_tiers_and_saturated_speed(self) -> None:
        original = [ranking_item(index) for index in range(1, 81)]
        shuffled = copy.deepcopy(original)
        random.Random(42).shuffle(shuffled)
        first, first_meta = ranking.rank_items(copy.deepcopy(original))
        second, second_meta = ranking.rank_items(shuffled)
        first_order = [row["recipe_hash"] for row in first if row.get("rank")]
        second_order = [row["recipe_hash"] for row in second if row.get("rank")]
        self.assertEqual(first_order, second_order)
        self.assertEqual(first_meta["counts"]["finalist"], 12)
        self.assertEqual(first_meta["counts"]["shortlist"], 32)
        self.assertEqual(first_meta["counts"]["reserve"], 64)
        self.assertFalse(first_meta["families"]["realtime_headroom"]["active"])
        self.assertIn("pairwise_median_improvement_ratio", first_meta["baseline_comparison"])
        top12 = [row for row in first if row.get("rank") and row["rank"] <= 12]
        self.assertEqual(len({row["archive_cell"] for row in top12}), 12)
        self.assertEqual(len({row["cluster_id"] for row in top12}), 12)

    def test_empty_input(self) -> None:
        rows, metadata = ranking.rank_items([])
        self.assertEqual(rows, [])
        self.assertEqual(metadata["counts"]["eligible"], 0)


class CertificationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows, self.metadata = ranking.rank_items(
            [ranking_item(index) for index in range(1, 81)]
        )

    def test_candidate_json_keeps_certification(self) -> None:
        clean = ranking.sanitized_item(self.rows[0])
        self.assertTrue(clean["performance_certification"]["certified"])
        self.assertEqual(clean["derived_metrics"]["certified_p95_fps"], 200.0)
        self.assertEqual(self.metadata["counts"]["performance_certified"], 80)

    def test_html_shows_certified_p95(self) -> None:
        payload = {
            "metadata": {
                "counts": self.metadata["counts"],
                "calibration": {"families": self.metadata["families"]},
                "baseline_comparison": self.metadata["baseline_comparison"],
                "generated_at": "2026-07-20T00:00:00Z",
            },
            "candidates": self.rows,
        }
        html_report = ranking.build_html(payload, Path("/tmp"), Path("/tmp"))
        self.assertIn("CERTIFIED · metal · 960×540", html_report)
        self.assertIn("p95 5.000 ms", html_report)
        self.assertIn("80件 960×540 Metal 30fps認証", html_report)

    def test_csv_contains_certification_columns(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "ranking.csv"
            ranking.write_csv(output, [ranking.sanitized_item(self.rows[0])])
            with output.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["certified"], "True")
        self.assertEqual(row["cert_backend"], "metal")
        self.assertEqual(row["cert_width"], "960")
        self.assertEqual(row["cert_p95_ms"], "5.0")


class ImageAnalysisAdapterTests(unittest.TestCase):
    def test_silently_omitted_row_fails_closed(self) -> None:
        with self.assertRaises(image_analysis.AnalysisError):
            image_analysis.index_validated_rows(
                [{"name": "a", "metric": 1.0}], {"a", "b"}, ("metric",), "fake analyzer"
            )

    def test_nonfinite_metric_fails_closed(self) -> None:
        with self.assertRaises(image_analysis.AnalysisError):
            image_analysis.index_validated_rows(
                [{"name": "a", "metric": float("nan")}], {"a"}, ("metric",), "fake analyzer"
            )


if __name__ == "__main__":
    unittest.main()
