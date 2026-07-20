#!/usr/bin/env python3
"""Compare original_metal_visual previews against the CPU reference lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np


SCHEMA = "glic-original-metal-reference-comparison-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-dir", type=Path, required=True)
    parser.add_argument("--metal-dir", type=Path, required=True)
    parser.add_argument("--cpu-benchmark", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--cdf-max-mae", type=float, default=24.0)
    parser.add_argument("--cdf-min-luma-ssim", type=float, default=0.75)
    parser.add_argument("--cdf-min-edge-correlation", type=float, default=0.60)
    parser.add_argument("--cdf-min-blurred-ssim", type=float, default=0.45)
    parser.add_argument(
        "--cdf-min-style-edge-correlation", type=float, default=0.25
    )
    parser.add_argument("--cdf-min-orientation-cosine", type=float, default=0.98)
    parser.add_argument("--cdf-min-edge-energy-ratio", type=float, default=0.75)
    parser.add_argument("--cdf-max-edge-energy-ratio", type=float, default=1.34)
    return parser.parse_args()


def fnv1a64_bytes(data: bytes) -> str:
    value = 14695981039346656037
    for byte in data:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def read_preview(path: Path) -> tuple[np.ndarray, str, str]:
    data = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"cannot decode image: {path}")
    if image.ndim != 3 or image.shape[2] < 3:
        raise RuntimeError(f"image is not RGB/BGRA: {path}")
    return image[:, :, :3], fnv1a64_bytes(data), hashlib.sha256(data).hexdigest()


def read_json_artifact(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"benchmark JSON root is not an object: {path}")
    return payload, hashlib.sha256(data).hexdigest()


def validate_fnv1a64(value: Any, context: str) -> str:
    if not isinstance(value, str) or len(value) != 16:
        raise RuntimeError(f"{context} is not a 16-digit FNV-1a64 hash")
    try:
        int(value, 16)
    except ValueError as error:
        raise RuntimeError(
            f"{context} is not a 16-digit FNV-1a64 hash"
        ) from error
    return value.lower()


def validate_benchmark(
    benchmark: Any, expected_schema: str, label: str
) -> list[dict[str, Any]]:
    if not isinstance(benchmark, dict) or benchmark.get("schema") != expected_schema:
        raise RuntimeError(f"{label} benchmark has the wrong schema")
    for field in (
        "input_decoded_color_fnv1a64",
        "width",
        "height",
        "frames",
        "warmup_frames",
        "output_preview_semantics",
        "output_preview_frame_index",
    ):
        if field not in benchmark or benchmark[field] in (None, ""):
            raise RuntimeError(f"{label} benchmark is missing provenance field: {field}")
    validate_fnv1a64(
        benchmark["input_decoded_color_fnv1a64"],
        f"{label} input_decoded_color_fnv1a64",
    )
    results = benchmark.get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError(f"{label} benchmark contains no preset results")
    if not all(isinstance(row, dict) for row in results):
        raise RuntimeError(f"{label} benchmark contains an invalid preset result")
    for index, row in enumerate(results):
        validate_fnv1a64(
            row.get("preset_config_fnv1a64"),
            f"{label} result {index} preset_config_fnv1a64",
        )
        validate_fnv1a64(
            row.get("output_preview_file_fnv1a64"),
            f"{label} result {index} output_preview_file_fnv1a64",
        )
    return results


def global_ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    a = reference.astype(np.float64)
    b = candidate.astype(np.float64)
    mean_a = float(a.mean())
    mean_b = float(b.mean())
    centered_a = a - mean_a
    centered_b = b - mean_b
    variance_a = float(np.mean(centered_a * centered_a))
    variance_b = float(np.mean(centered_b * centered_b))
    covariance = float(np.mean(centered_a * centered_b))
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    denominator = (mean_a * mean_a + mean_b * mean_b + c1) * (
        variance_a + variance_b + c2
    )
    if denominator <= 0.0:
        return 1.0
    return float(
        ((2.0 * mean_a * mean_b + c1) * (2.0 * covariance + c2))
        / denominator
    )


def edge_magnitude(luma: np.ndarray) -> np.ndarray:
    source = luma.astype(np.float32)
    dx = cv2.Sobel(source, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(source, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(dx, dy)


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    flat_a = a.reshape(-1).astype(np.float64)
    flat_b = b.reshape(-1).astype(np.float64)
    std_a = float(flat_a.std())
    std_b = float(flat_b.std())
    if std_a <= 1e-12 or std_b <= 1e-12:
        return 1.0 if np.array_equal(flat_a, flat_b) else 0.0
    value = float(np.corrcoef(flat_a, flat_b)[0, 1])
    return value if math.isfinite(value) else 0.0


def orientation_histogram(luma: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    source = luma.astype(np.float32)
    dx = cv2.Sobel(source, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(source, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(dx, dy)
    angle = np.mod(cv2.phase(dx, dy, angleInDegrees=False), np.pi)
    histogram, _ = np.histogram(
        angle, bins=18, range=(0.0, np.pi), weights=magnitude
    )
    histogram = histogram.astype(np.float64)
    histogram /= float(histogram.sum()) + 1e-12
    return histogram, magnitude


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 1e-12:
        return 1.0 if np.array_equal(a, b) else 0.0
    return float(np.dot(a, b) / denominator)


def compare_images(reference: np.ndarray, candidate: np.ndarray) -> dict[str, Any]:
    if reference.shape != candidate.shape:
        raise RuntimeError(
            f"image shape mismatch: {reference.shape} != {candidate.shape}"
        )
    delta = np.abs(reference.astype(np.int16) - candidate.astype(np.int16))
    changed_pixels = np.any(delta != 0, axis=2)
    luma_reference = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    luma_candidate = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
    orientation_reference, edge_reference = orientation_histogram(luma_reference)
    orientation_candidate, edge_candidate = orientation_histogram(luma_candidate)
    blurred_reference = cv2.GaussianBlur(luma_reference, (0, 0), 32.0)
    blurred_candidate = cv2.GaussianBlur(luma_candidate, (0, 0), 32.0)
    edge_reference_mean = float(edge_reference.mean())
    edge_energy_ratio = (
        float(edge_candidate.mean()) / edge_reference_mean
        if edge_reference_mean > 1e-12
        else 1.0
    )
    return {
        "mae_rgb": round(float(delta.mean()), 6),
        "p95_channel_error": round(float(np.percentile(delta, 95)), 6),
        "max_channel_error": int(delta.max()),
        "exact_channel_ratio": round(float(np.mean(delta == 0)), 9),
        "exact_pixel_ratio": round(float(np.mean(~changed_pixels)), 9),
        "changed_pixel_ratio": round(float(np.mean(changed_pixels)), 9),
        "luma_ssim": round(global_ssim(luma_reference, luma_candidate), 9),
        "edge_correlation": round(
            correlation(edge_reference, edge_candidate),
            9,
        ),
        "blurred_luma_ssim_sigma32": round(
            global_ssim(blurred_reference, blurred_candidate), 9
        ),
        "edge_orientation_cosine": round(
            cosine_similarity(orientation_reference, orientation_candidate), 9
        ),
        "edge_energy_ratio": round(edge_energy_ratio, 9),
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# original_metal_visual CPU reference comparison",
        "",
        f"Numeric reference: **{'PASS' if report['reference_match_passed'] else 'FAIL'}**",
        "",
        f"Original-style morphology: **{'PASS' if report['original_style_match_passed'] else 'FAIL'}**",
        "",
        "| Preset | Tier | MAE | Luma SSIM | Blur SSIM | Orientation | Exact pixels | Numeric | Style |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["results"]:
        lines.append(
            f"| {row['preset']} | {'CDF97' if row['uses_cdf97'] else 'integer'} "
            f"| {row['mae_rgb']:.3f} | {row['luma_ssim']:.4f} "
            f"| {row['blurred_luma_ssim_sigma32']:.4f} "
            f"| {row['edge_orientation_cosine']:.4f} "
            f"| {row['exact_pixel_ratio']:.4f} "
            f"| {'PASS' if row['reference_match_passed'] else 'FAIL'} "
            f"| {'PASS' if row['original_style_match_passed'] else 'FAIL'} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    benchmark, benchmark_sha256 = read_json_artifact(args.benchmark)
    cpu_benchmark, cpu_benchmark_sha256 = read_json_artifact(
        args.cpu_benchmark
    )
    benchmark_results = validate_benchmark(
        benchmark, "glic-original-realtime-metal-benchmark-v1", "Metal"
    )
    cpu_benchmark_results = validate_benchmark(
        cpu_benchmark, "glic-original-realtime-cpu-benchmark-v1", "CPU"
    )
    provenance_fields = (
        "input_decoded_color_fnv1a64",
        "width",
        "height",
        "frames",
        "warmup_frames",
        "output_preview_semantics",
        "output_preview_frame_index",
    )
    for field in provenance_fields:
        if benchmark[field] != cpu_benchmark[field]:
            raise RuntimeError(f"CPU/Metal benchmark provenance mismatch: {field}")

    def preset_map(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            preset = row.get("preset")
            if not isinstance(preset, str) or not preset:
                raise RuntimeError(f"{label} benchmark contains an invalid preset name")
            if preset in output:
                raise RuntimeError(f"{label} benchmark contains duplicate preset: {preset}")
            output[preset] = row
        return output

    metal_presets = preset_map(benchmark_results, "Metal")
    cpu_presets = preset_map(cpu_benchmark_results, "CPU")
    if metal_presets.keys() != cpu_presets.keys():
        raise RuntimeError("CPU/Metal benchmark preset sets differ")
    for preset, row in metal_presets.items():
        metal_config_hash = validate_fnv1a64(
            row.get("preset_config_fnv1a64"),
            f"Metal preset configuration {preset}",
        )
        cpu_config_hash = validate_fnv1a64(
            cpu_presets[preset].get("preset_config_fnv1a64"),
            f"CPU preset configuration {preset}",
        )
        if metal_config_hash != cpu_config_hash:
            raise RuntimeError(f"CPU/Metal preset configuration differs: {preset}")
        if bool(row.get("uses_cdf97")) != bool(
            cpu_presets[preset].get("uses_cdf97")
        ):
            raise RuntimeError(f"CPU/Metal transform tier differs: {preset}")

    results: list[dict[str, Any]] = []
    for benchmark_row in benchmark_results:
        preset = benchmark_row.get("preset")
        if not isinstance(preset, str) or not preset:
            raise RuntimeError("benchmark contains an invalid preset name")
        reference_path = args.cpu_dir / f"{preset}.png"
        candidate_path = args.metal_dir / f"{preset}.png"
        cpu_row = cpu_presets[preset]
        reference, cpu_preview_fnv, cpu_preview_sha256 = read_preview(
            reference_path
        )
        candidate, metal_preview_fnv, metal_preview_sha256 = read_preview(
            candidate_path
        )
        expected_cpu_preview_fnv = validate_fnv1a64(
            cpu_row.get("output_preview_file_fnv1a64"),
            f"CPU preview provenance {preset}",
        )
        expected_metal_preview_fnv = validate_fnv1a64(
            benchmark_row.get("output_preview_file_fnv1a64"),
            f"Metal preview provenance {preset}",
        )
        if cpu_preview_fnv != expected_cpu_preview_fnv:
            raise RuntimeError(f"CPU preview FNV-1a64 mismatch: {preset}")
        if metal_preview_fnv != expected_metal_preview_fnv:
            raise RuntimeError(f"Metal preview FNV-1a64 mismatch: {preset}")
        metrics = compare_images(reference, candidate)
        uses_cdf97 = bool(benchmark_row.get("uses_cdf97"))
        if uses_cdf97:
            numeric_passed = (
                metrics["mae_rgb"] <= args.cdf_max_mae
                and metrics["luma_ssim"] >= args.cdf_min_luma_ssim
                and metrics["edge_correlation"] >= args.cdf_min_edge_correlation
            )
            style_passed = numeric_passed or (
                metrics["blurred_luma_ssim_sigma32"]
                >= args.cdf_min_blurred_ssim
                and metrics["edge_correlation"]
                >= args.cdf_min_style_edge_correlation
                and metrics["edge_orientation_cosine"]
                >= args.cdf_min_orientation_cosine
                and metrics["edge_energy_ratio"]
                >= args.cdf_min_edge_energy_ratio
                and metrics["edge_energy_ratio"]
                <= args.cdf_max_edge_energy_ratio
            )
        else:
            numeric_passed = metrics["max_channel_error"] == 0
            style_passed = numeric_passed
        results.append(
            {
                "preset": preset,
                "uses_cdf97": uses_cdf97,
                "preset_config_fnv1a64": benchmark_row[
                    "preset_config_fnv1a64"
                ],
                "reference_match_passed": numeric_passed,
                "original_style_match_passed": style_passed,
                "cpu_preview": str(reference_path.resolve()),
                "cpu_preview_fnv1a64": cpu_preview_fnv,
                "cpu_preview_sha256": cpu_preview_sha256,
                "metal_preview": str(candidate_path.resolve()),
                "metal_preview_fnv1a64": metal_preview_fnv,
                "metal_preview_sha256": metal_preview_sha256,
                **metrics,
            }
        )

    failed = [row["preset"] for row in results if not row["reference_match_passed"]]
    style_failed = [
        row["preset"] for row in results if not row["original_style_match_passed"]
    ]
    report = {
        "schema": SCHEMA,
        "benchmark": str(args.benchmark.resolve()),
        "benchmark_sha256": benchmark_sha256,
        "cpu_benchmark": str(args.cpu_benchmark.resolve()),
        "cpu_benchmark_sha256": cpu_benchmark_sha256,
        "provenance_match_verified": True,
        "input_path": benchmark.get("input_path"),
        "input_decoded_color_fnv1a64": benchmark.get(
            "input_decoded_color_fnv1a64"
        ),
        "output_preview_semantics": benchmark.get("output_preview_semantics"),
        "output_preview_frame_index": benchmark.get(
            "output_preview_frame_index"
        ),
        "cpu_directory": str(args.cpu_dir.resolve()),
        "metal_directory": str(args.metal_dir.resolve()),
        "policy": {
            "no_wavelet_requires_pixel_exact": True,
            "cdf97_max_mae_rgb": args.cdf_max_mae,
            "cdf97_min_luma_ssim": args.cdf_min_luma_ssim,
            "cdf97_min_edge_correlation": args.cdf_min_edge_correlation,
            "cdf97_min_blurred_luma_ssim_sigma32": args.cdf_min_blurred_ssim,
            "cdf97_min_style_edge_correlation": (
                args.cdf_min_style_edge_correlation
            ),
            "cdf97_min_edge_orientation_cosine": args.cdf_min_orientation_cosine,
            "cdf97_edge_energy_ratio_range": [
                args.cdf_min_edge_energy_ratio,
                args.cdf_max_edge_energy_ratio,
            ],
        },
        "preset_count": len(results),
        "passed_count": len(results) - len(failed),
        "failed_count": len(failed),
        "failed_presets": failed,
        "reference_match_passed": not failed,
        "style_passed_count": len(results) - len(style_failed),
        "style_failed_count": len(style_failed),
        "style_failed_presets": style_failed,
        "original_style_match_passed": not style_failed,
        "results": results,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.output_md is not None:
        write_markdown(args.output_md, report)
    print(
        f"numeric_reference={'PASS' if not failed else 'FAIL'} "
        f"numeric={report['passed_count']}/{report['preset_count']} "
        f"original_style={'PASS' if not style_failed else 'FAIL'} "
        f"style={report['style_passed_count']}/{report['preset_count']} "
        f"report={args.output_json}"
    )
    return 0 if not style_failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", flush=True)
        raise SystemExit(2)
