#!/usr/bin/env python3

from __future__ import annotations

import copy
import random
import sys
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
    return {
        "candidate_id": str(index),
        "recipe_hash": f"{index:016x}",
        "preview_hash": f"{index + 1000:016x}",
        "evaluation_hash": f"{index + 2000:016x}",
        "archive_cell": f"cell-{index}",
        "generation": "mutation",
        "parent_hash": "",
        "recipe_family": f"family-{index}",
        "color_space": index % 16,
        "eligible": True,
        "hard_gate": {"passed": True, "reasons": []},
        "raw_metrics": metric,
        "liveliness": live,
        "perceptual": visual,
        "derived_metrics": {
            "mean_fps": 1000.0 / metric["mean_process_ms"],
            "input_consistency": consistency,
            "abs_luma_correlation": abs(metric["luma_correlation"]),
            "unclipped": 1.0 - metric["clipping_ratio"],
        },
        "search_quality": 0.5 + index / 1000.0,
        "preview_path": f"elites/{index:016x}.png",
        "recipe": {
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
        },
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
                "mean_process_ms": 1000.0 / 15.0,
            }
        )
        self.assertEqual(ranking.hard_gate_reasons(metric), [])

    def test_each_gate_rejects(self) -> None:
        cases = {
            "below_15_fps": ("mean_process_ms", 67.0),
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
