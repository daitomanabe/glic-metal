#!/usr/bin/env python3
"""Build a fail-closed Fast Match preset allowlist from paired benchmarks."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np


SCHEMA = "glic-fast-match-allowlist-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        action="append",
        nargs=5,
        metavar=("LABEL", "STRICT_JSON", "FAST_JSON", "STRICT_DIR", "FAST_DIR"),
        required=True,
        help="paired strict/fast benchmark and preview directories; repeatable",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--min-windowed-ssim", type=float, default=0.94)
    parser.add_argument("--max-mean-delta-e", type=float, default=6.0)
    parser.add_argument("--max-edge-disagreement", type=float, default=0.12)
    parser.add_argument("--max-clipping-increase", type=float, default=0.02)
    parser.add_argument("--max-p95-ms", type=float, default=50.0)
    parser.add_argument("--min-fps", type=float, default=20.0)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return payload


def result_map(payload: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise RuntimeError(f"{label} benchmark has no results array")
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("preset"), str):
            raise RuntimeError(f"{label} benchmark has an invalid result")
        mapped[row["preset"]] = row
    return mapped


def finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{context} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"{context} is not finite")
    return number


def read_image(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"cannot decode preview: {path}")
    return image


def windowed_luma_ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    a = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mean_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mean_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    variance_a = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mean_a * mean_a
    variance_b = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mean_b * mean_b
    covariance = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mean_a * mean_b
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    numerator = (2.0 * mean_a * mean_b + c1) * (2.0 * covariance + c2)
    denominator = (mean_a * mean_a + mean_b * mean_b + c1) * (
        variance_a + variance_b + c2
    )
    return float(np.mean(numerator / np.maximum(denominator, 1e-12)))


def image_metrics(reference: np.ndarray, candidate: np.ndarray) -> dict[str, float]:
    if reference.shape != candidate.shape:
        raise RuntimeError(
            f"preview shape mismatch: {reference.shape} != {candidate.shape}"
        )
    reference_lab = cv2.cvtColor(
        reference.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB
    )
    candidate_lab = cv2.cvtColor(
        candidate.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB
    )
    mean_delta_e = float(
        np.mean(np.linalg.norm(reference_lab - candidate_lab, axis=2))
    )
    reference_luma = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    candidate_luma = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
    reference_edges = cv2.Canny(reference_luma, 64, 128, L2gradient=True) > 0
    candidate_edges = cv2.Canny(candidate_luma, 64, 128, L2gradient=True) > 0
    edge_disagreement = float(np.mean(np.logical_xor(reference_edges, candidate_edges)))
    reference_clipped = np.logical_or(reference <= 8, reference >= 247)
    candidate_clipped = np.logical_or(candidate <= 8, candidate >= 247)
    clipping_increase = max(
        0.0, float(np.mean(candidate_clipped) - np.mean(reference_clipped))
    )
    exact_pixel_ratio = float(np.mean(np.all(reference == candidate, axis=2)))
    return {
        "windowed_luma_ssim": windowed_luma_ssim(reference, candidate),
        "mean_delta_e76": mean_delta_e,
        "edge_disagreement_ratio": edge_disagreement,
        "clipping_increase_ratio": clipping_increase,
        "exact_pixel_ratio": exact_pixel_ratio,
    }


def main() -> int:
    args = parse_args()
    thresholds = {
        "minimum_windowed_luma_ssim": args.min_windowed_ssim,
        "maximum_mean_delta_e76": args.max_mean_delta_e,
        "maximum_edge_disagreement_ratio": args.max_edge_disagreement,
        "maximum_clipping_increase_ratio": args.max_clipping_increase,
        "maximum_p95_ms": args.max_p95_ms,
        "minimum_fps": args.min_fps,
    }
    by_preset: dict[str, dict[str, Any]] = {}
    case_labels: list[str] = []

    for raw_case in args.case:
        label, strict_json, fast_json, strict_dir, fast_dir = raw_case
        case_labels.append(label)
        strict_payload = load_json(Path(strict_json))
        fast_payload = load_json(Path(fast_json))
        if strict_payload.get("input_decoded_color_fnv1a64") != fast_payload.get(
            "input_decoded_color_fnv1a64"
        ):
            raise RuntimeError(f"{label}: strict and fast inputs differ")
        if strict_payload.get("fidelity_mode") != "strict":
            raise RuntimeError(f"{label}: strict benchmark is not strict")
        if fast_payload.get("fidelity_mode") != "fast-match":
            raise RuntimeError(f"{label}: fast benchmark is not fast-match")
        strict_results = result_map(strict_payload, f"{label} strict")
        fast_results = result_map(fast_payload, f"{label} fast")
        if strict_results.keys() != fast_results.keys():
            raise RuntimeError(f"{label}: preset result sets differ")

        for preset in sorted(strict_results):
            strict_row = strict_results[preset]
            fast_row = fast_results[preset]
            if strict_row.get("preset_config_fnv1a64") != fast_row.get(
                "preset_config_fnv1a64"
            ):
                raise RuntimeError(f"{label}/{preset}: preset configs differ")
            metrics = image_metrics(
                read_image(Path(strict_dir) / f"{preset}.png"),
                read_image(Path(fast_dir) / f"{preset}.png"),
            )
            fast_p95 = finite_number(fast_row.get("p95_ms"), f"{label}/{preset} p95")
            fast_fps = finite_number(fast_row.get("fps"), f"{label}/{preset} fps")
            strict_mean = finite_number(
                strict_row.get("mean_ms"), f"{label}/{preset} strict mean"
            )
            fast_mean = finite_number(
                fast_row.get("mean_ms"), f"{label}/{preset} fast mean"
            )
            metrics.update(
                {
                    "fast_p95_ms": fast_p95,
                    "fast_fps": fast_fps,
                    "speedup_ratio": strict_mean / max(fast_mean, 1e-12),
                }
            )
            by_preset.setdefault(
                preset,
                {
                    "preset": preset,
                    "preset_config_fnv1a64": strict_row["preset_config_fnv1a64"],
                    "cases": [],
                },
            )["cases"].append({"label": label, **metrics})

    results: list[dict[str, Any]] = []
    allowlist: list[str] = []
    for preset in sorted(by_preset):
        row = by_preset[preset]
        cases = row["cases"]
        aggregate = {
            "minimum_windowed_luma_ssim": min(c["windowed_luma_ssim"] for c in cases),
            "maximum_mean_delta_e76": max(c["mean_delta_e76"] for c in cases),
            "maximum_edge_disagreement_ratio": max(
                c["edge_disagreement_ratio"] for c in cases
            ),
            "maximum_clipping_increase_ratio": max(
                c["clipping_increase_ratio"] for c in cases
            ),
            "maximum_fast_p95_ms": max(c["fast_p95_ms"] for c in cases),
            "minimum_fast_fps": min(c["fast_fps"] for c in cases),
            "minimum_speedup_ratio": min(c["speedup_ratio"] for c in cases),
        }
        reasons: list[str] = []
        if aggregate["minimum_windowed_luma_ssim"] < args.min_windowed_ssim:
            reasons.append("windowed_luma_ssim")
        if aggregate["maximum_mean_delta_e76"] > args.max_mean_delta_e:
            reasons.append("mean_delta_e76")
        if aggregate["maximum_edge_disagreement_ratio"] > args.max_edge_disagreement:
            reasons.append("edge_disagreement")
        if aggregate["maximum_clipping_increase_ratio"] > args.max_clipping_increase:
            reasons.append("clipping_increase")
        if aggregate["maximum_fast_p95_ms"] > args.max_p95_ms:
            reasons.append("p95_performance")
        if aggregate["minimum_fast_fps"] < args.min_fps:
            reasons.append("fps_performance")
        allowed = not reasons
        if allowed:
            allowlist.append(preset)
        results.append({**row, "aggregate": aggregate, "allowed": allowed, "reasons": reasons})

    payload = {
        "schema": SCHEMA,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "policy": "all-cases-must-pass",
        "cases": case_labels,
        "thresholds": thresholds,
        "allowlist": allowlist,
        "results": results,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.output_md:
        lines = [
            "# Fast Match allowlist",
            "",
            f"Allowed: {len(allowlist)} / {len(results)} presets",
            "",
            "| Preset | Allowed | min SSIM | max Delta E | max edge diff | p95 ms | min fps | min speedup |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in results:
            a = row["aggregate"]
            lines.append(
                f"| {row['preset']} | {'yes' if row['allowed'] else 'no'} | "
                f"{a['minimum_windowed_luma_ssim']:.4f} | "
                f"{a['maximum_mean_delta_e76']:.3f} | "
                f"{a['maximum_edge_disagreement_ratio']:.4f} | "
                f"{a['maximum_fast_p95_ms']:.3f} | "
                f"{a['minimum_fast_fps']:.2f} | "
                f"{a['minimum_speedup_ratio']:.3f} |"
            )
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"allowed={len(allowlist)} total={len(results)} output={args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
