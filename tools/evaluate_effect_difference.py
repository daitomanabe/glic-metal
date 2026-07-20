#!/usr/bin/env python3
"""Measure whether processed video frames meaningfully differ from a source."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

import cv2
import numpy as np


METRIC_NAMES = (
    "rgb_mae_255",
    "rgb_rmse_255",
    "luma_mae_255",
    "delta_e76_mean",
    "changed_ratio_5",
    "changed_ratio_10",
    "changed_ratio_20",
    "changed_ratio_40",
    "ssim_luma",
    "edge_disagreement_ratio",
    "effect_temporal_delta_255",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare aligned processed videos against a source and a passthrough "
            "re-encode control."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="LABEL=VIDEO",
        help="Candidate label and path; repeat for multiple outputs.",
    )
    parser.add_argument("--analysis-width", type=int, default=810)
    parser.add_argument("--sample-every", type=int, default=1)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--heatmap", type=Path, required=True)
    return parser.parse_args()


def parse_candidate(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"Candidate must be LABEL=VIDEO: {value}")
    label, path_text = value.split("=", 1)
    if not label or not path_text:
        raise ValueError(f"Candidate must be LABEL=VIDEO: {value}")
    return label, Path(path_text).expanduser().resolve()


def resize_for_analysis(frame: np.ndarray, width: int) -> np.ndarray:
    if width <= 0 or frame.shape[1] <= width:
        return frame
    height = max(1, round(frame.shape[0] * width / frame.shape[1]))
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def luma(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)


def ssim_luma(first: np.ndarray, second: np.ndarray) -> float:
    first_luma = luma(first)
    second_luma = luma(second)
    constant1 = (0.01 * 255.0) ** 2
    constant2 = (0.03 * 255.0) ** 2
    mean1 = cv2.GaussianBlur(first_luma, (11, 11), 1.5)
    mean2 = cv2.GaussianBlur(second_luma, (11, 11), 1.5)
    mean1_squared = mean1 * mean1
    mean2_squared = mean2 * mean2
    mean_product = mean1 * mean2
    variance1 = cv2.GaussianBlur(first_luma * first_luma, (11, 11), 1.5)
    variance1 -= mean1_squared
    variance2 = cv2.GaussianBlur(second_luma * second_luma, (11, 11), 1.5)
    variance2 -= mean2_squared
    covariance = cv2.GaussianBlur(first_luma * second_luma, (11, 11), 1.5)
    covariance -= mean_product
    numerator = (2.0 * mean_product + constant1) * (2.0 * covariance + constant2)
    denominator = (mean1_squared + mean2_squared + constant1) * (
        variance1 + variance2 + constant2
    )
    return float(np.mean(numerator / np.maximum(denominator, 1.0e-9)))


def delta_e76(first: np.ndarray, second: np.ndarray) -> float:
    first_lab = cv2.cvtColor(first.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    second_lab = cv2.cvtColor(second.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    return float(np.linalg.norm(first_lab - second_lab, axis=2).mean())


def edge_disagreement(first_luma: np.ndarray, second_luma: np.ndarray) -> float:
    first_edges = cv2.Canny(first_luma.astype(np.uint8), 80, 160) > 0
    second_edges = cv2.Canny(second_luma.astype(np.uint8), 80, 160) > 0
    edge_union = np.logical_or(first_edges, second_edges)
    if not np.any(edge_union):
        return 0.0
    return float(np.logical_xor(first_edges, second_edges).sum() / edge_union.sum())


def compare_frames(
    first: np.ndarray, second: np.ndarray, previous_effect: np.ndarray | None
) -> tuple[dict[str, float], np.ndarray]:
    first_float = first.astype(np.float32)
    second_float = second.astype(np.float32)
    absolute = np.abs(second_float - first_float)
    maximum_channel_difference = absolute.max(axis=2)
    first_luma = luma(first)
    second_luma = luma(second)
    signed_effect = second_luma - first_luma
    temporal_delta = (
        0.0
        if previous_effect is None
        else float(np.abs(signed_effect - previous_effect).mean())
    )

    metrics = {
        "rgb_mae_255": float(absolute.mean()),
        "rgb_rmse_255": float(np.sqrt(np.mean(np.square(absolute)))),
        "luma_mae_255": float(np.abs(signed_effect).mean()),
        "delta_e76_mean": delta_e76(first, second),
        "changed_ratio_5": float(np.mean(maximum_channel_difference >= 5.0)),
        "changed_ratio_10": float(np.mean(maximum_channel_difference >= 10.0)),
        "changed_ratio_20": float(np.mean(maximum_channel_difference >= 20.0)),
        "changed_ratio_40": float(np.mean(maximum_channel_difference >= 40.0)),
        "ssim_luma": ssim_luma(first, second),
        "edge_disagreement_ratio": edge_disagreement(first_luma, second_luma),
        "effect_temporal_delta_255": temporal_delta,
    }
    return metrics, signed_effect


def summarize(values: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name in METRIC_NAMES:
        data = np.asarray(values[name], dtype=np.float64)
        result[name] = {
            "mean": float(np.mean(data)),
            "median": float(np.median(data)),
            "p95": float(np.percentile(data, 95)),
            "max": float(np.max(data)),
        }
    return result


def classify(metrics: dict[str, dict[str, float]]) -> tuple[str, bool]:
    mae = metrics["rgb_mae_255"]["mean"]
    changed10 = metrics["changed_ratio_10"]["mean"]
    changed20 = metrics["changed_ratio_20"]["mean"]
    ssim = metrics["ssim_luma"]["mean"]
    if mae >= 20.0 and changed20 >= 0.35 and ssim <= 0.85:
        return "STRONG", True
    if mae >= 10.0 and changed10 >= 0.25 and ssim <= 0.95:
        return "VISIBLE", True
    if mae >= 4.0 or changed10 >= 0.10 or ssim <= 0.98:
        return "SUBTLE", False
    return "MINIMAL", False


def labeled_panel(frame: np.ndarray, label: str) -> np.ndarray:
    panel = frame.copy()
    cv2.rectangle(panel, (0, 0), (panel.shape[1], 44), (0, 0, 0), -1)
    cv2.putText(
        panel,
        label,
        (12, 31),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return panel


def difference_panels(
    source: np.ndarray, compared: np.ndarray, label: str
) -> list[np.ndarray]:
    difference = cv2.absdiff(source, compared)
    maximum = difference.max(axis=2)
    amplified = np.clip(maximum.astype(np.float32) * 8.0, 0, 255).astype(np.uint8)
    heatmap = cv2.applyColorMap(amplified, cv2.COLORMAP_TURBO)
    mask = np.where(maximum >= 10, 255, 0).astype(np.uint8)
    mask_panel = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    return [
        labeled_panel(source, "SOURCE"),
        labeled_panel(compared, label),
        labeled_panel(heatmap, "ABS DIFF x8"),
        labeled_panel(mask_panel, "MAX CHANNEL DIFF >= 10"),
    ]


def write_markdown(path: Path, report: dict) -> None:
    lines = [
        "# GLIC effect difference report",
        "",
        (
            f"Analyzed {report['analyzed_frames']} aligned frames at "
            f"{report['analysis_width']} px width."
        ),
        "",
        "| Output | Verdict | MAE vs control | DeltaE76 | Pixels >=10 | Pixels >=20 | SSIM | Edge disagreement |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for candidate in report["candidates"]:
        metrics = candidate["vs_control"]
        lines.append(
            "| {label} | {verdict} | {mae:.2f} | {delta:.2f} | {ratio10:.1%} | "
            "{ratio20:.1%} | {ssim:.4f} | {edge:.1%} |".format(
                label=candidate["label"],
                verdict=candidate["verdict"],
                mae=metrics["rgb_mae_255"]["mean"],
                delta=metrics["delta_e76_mean"]["mean"],
                ratio10=metrics["changed_ratio_10"]["mean"],
                ratio20=metrics["changed_ratio_20"]["mean"],
                ssim=metrics["ssim_luma"]["mean"],
                edge=metrics["edge_disagreement_ratio"]["mean"],
            )
        )
    lines.extend(
        [
            "",
            "The control is the same BGRA pipe and H.264 encode with no GLIC processing.",
            "A meaningful glitch passes at VISIBLE or STRONG. SUBTLE and MINIMAL fail the gate.",
            "The heatmap multiplies absolute differences by 8 only for display; numeric metrics are unamplified.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    if args.analysis_width <= 0 or args.sample_every <= 0:
        raise ValueError("analysis-width and sample-every must be positive")
    source_path = args.source.expanduser().resolve()
    control_path = args.control.expanduser().resolve()
    candidates = [parse_candidate(value) for value in args.candidate]
    paths = {"source": source_path, "control": control_path, **dict(candidates)}
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} video was not found: {path}")

    captures = {label: cv2.VideoCapture(str(path)) for label, path in paths.items()}
    for label, capture in captures.items():
        if not capture.isOpened():
            raise RuntimeError(f"Failed to open {label}: {paths[label]}")
    frame_counts = [int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) for capture in captures.values()]
    representative_index = max(0, min(frame_counts) // 2)

    values: dict[str, dict[str, list[float]]] = {
        "control_vs_source": defaultdict(list)
    }
    previous_effects: dict[str, np.ndarray | None] = {"control_vs_source": None}
    for label, _ in candidates:
        values[f"{label}_vs_source"] = defaultdict(list)
        values[f"{label}_vs_control"] = defaultdict(list)
        previous_effects[f"{label}_vs_source"] = None
        previous_effects[f"{label}_vs_control"] = None

    representative_frames: dict[str, np.ndarray] = {}
    decoded_frames = 0
    analyzed_frames = 0
    try:
        while True:
            decoded = {label: capture.read() for label, capture in captures.items()}
            successes = [success for success, _ in decoded.values()]
            if not any(successes):
                break
            if not all(successes):
                failed = [label for label, (success, _) in decoded.items() if not success]
                raise RuntimeError(
                    f"Frame count mismatch at frame {decoded_frames}; ended: {failed}"
                )
            frames = {
                label: resize_for_analysis(frame, args.analysis_width)
                for label, (_, frame) in decoded.items()
            }
            if decoded_frames == representative_index:
                representative_frames = {label: frame.copy() for label, frame in frames.items()}
            if decoded_frames % args.sample_every == 0:
                source = frames["source"]
                control = frames["control"]
                key = "control_vs_source"
                metrics, effect = compare_frames(
                    source, control, previous_effects[key]
                )
                for name, value in metrics.items():
                    values[key][name].append(value)
                previous_effects[key] = effect

                for label, _ in candidates:
                    for suffix, first, second in (
                        ("vs_source", source, frames[label]),
                        ("vs_control", control, frames[label]),
                    ):
                        key = f"{label}_{suffix}"
                        metrics, effect = compare_frames(
                            first, second, previous_effects[key]
                        )
                        for name, value in metrics.items():
                            values[key][name].append(value)
                        previous_effects[key] = effect
                analyzed_frames += 1
            decoded_frames += 1
    finally:
        for capture in captures.values():
            capture.release()

    if analyzed_frames < 2:
        raise RuntimeError("Fewer than two aligned frames were analyzed")
    if not representative_frames:
        raise RuntimeError("Representative frames were not captured")

    control_summary = summarize(values["control_vs_source"])
    candidate_reports = []
    for label, path in candidates:
        vs_source = summarize(values[f"{label}_vs_source"])
        vs_control = summarize(values[f"{label}_vs_control"])
        verdict, passed = classify(vs_control)
        candidate_reports.append(
            {
                "label": label,
                "path": str(path),
                "verdict": verdict,
                "meaningful_glitch_passed": passed,
                "vs_source": vs_source,
                "vs_control": vs_control,
                "mae_multiple_over_codec_baseline": (
                    vs_source["rgb_mae_255"]["mean"]
                    / max(control_summary["rgb_mae_255"]["mean"], 1.0e-9)
                ),
            }
        )

    report = {
        "schema": "glic-effect-difference-v1",
        "source": str(source_path),
        "control": str(control_path),
        "analysis_width": representative_frames["source"].shape[1],
        "analysis_height": representative_frames["source"].shape[0],
        "decoded_frames": decoded_frames,
        "analyzed_frames": analyzed_frames,
        "sample_every": args.sample_every,
        "gate": {
            "meaningful_verdicts": ["VISIBLE", "STRONG"],
            "visible": "MAE >= 10, pixels >=10 >= 25%, SSIM <= 0.95",
            "strong": "MAE >= 20, pixels >=20 >= 35%, SSIM <= 0.85",
        },
        "codec_baseline_vs_source": control_summary,
        "candidates": candidate_reports,
    }

    rows = []
    source_frame = representative_frames["source"]
    rows.append(
        cv2.hconcat(
            difference_panels(source_frame, representative_frames["control"], "CONTROL")
        )
    )
    for label, _ in candidates:
        rows.append(
            cv2.hconcat(
                difference_panels(source_frame, representative_frames[label], label)
            )
        )
    heatmap = cv2.vconcat(rows)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.heatmap.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2) + "\n")
    write_markdown(args.output_md, report)
    if not cv2.imwrite(str(args.heatmap), heatmap):
        raise RuntimeError(f"Failed to write heatmap: {args.heatmap}")

    for candidate in candidate_reports:
        metrics = candidate["vs_control"]
        print(
            f"{candidate['label']}: {candidate['verdict']} "
            f"mae={metrics['rgb_mae_255']['mean']:.3f} "
            f"changed10={metrics['changed_ratio_10']['mean']:.1%} "
            f"ssim={metrics['ssim_luma']['mean']:.4f}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
