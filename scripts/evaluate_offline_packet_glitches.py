#!/usr/bin/env python3
"""Evaluate packet glitches whose decoded outputs may have different lengths."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from evaluate_effect_difference import (  # noqa: E402
    METRIC_NAMES,
    classify,
    compare_frames,
    difference_panels,
    resize_for_analysis,
    summarize,
)


def parse_candidate(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"Candidate must be LABEL=VIDEO: {value}")
    label, path_text = value.split("=", 1)
    if not label or not path_text:
        raise ValueError(f"Candidate must be LABEL=VIDEO: {value}")
    return label, Path(path_text).expanduser().resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare packet-glitch previews at normalized timeline positions, "
            "including outputs that lost frames during salvage decode."
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
    parser.add_argument("--samples", type=int, default=24)
    parser.add_argument("--analysis-width", type=int, default=810)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--heatmap", type=Path, required=True)
    return parser.parse_args()


def video_info(path: Path) -> dict[str, float | int]:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Failed to open video: {path}")
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if frames < 1:
        raise RuntimeError(f"Video has no decodable frames: {path}")
    return {
        "frames": frames,
        "fps": fps if np.isfinite(fps) and fps > 0 else 0.0,
        "width": width,
        "height": height,
        "duration_seconds": frames / fps if fps > 0 else 0.0,
    }


def read_normalized_frame(
    capture: cv2.VideoCapture,
    frame_count: int,
    position: float,
    analysis_width: int,
    target_size: tuple[int, int] | None = None,
) -> np.ndarray:
    index = round(position * max(0, frame_count - 1))
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    success, frame = capture.read()
    if not success:
        # Some damaged-preview decoders reject an exact random seek near EOF.
        for fallback in range(index - 1, max(-1, index - 4), -1):
            capture.set(cv2.CAP_PROP_POS_FRAMES, fallback)
            success, frame = capture.read()
            if success:
                break
    if not success:
        raise RuntimeError(
            f"Could not decode normalized position {position:.3f} "
            f"(frame {index}/{frame_count})"
        )
    frame = resize_for_analysis(frame, analysis_width)
    if target_size and (frame.shape[1], frame.shape[0]) != target_size:
        frame = cv2.resize(frame, target_size, interpolation=cv2.INTER_AREA)
    return frame


def packet_report(path: Path) -> dict:
    adjacent = path.with_suffix(path.suffix + ".json")
    if not adjacent.is_file():
        return {}
    try:
        report = json.loads(adjacent.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return report if report.get("schema") == "glic-offline-packet-glitch-v1" else {}


def ranking_score(
    metrics: dict[str, dict[str, float]], survival_ratio: float
) -> float:
    mae = min(50.0, metrics["rgb_mae_255"]["mean"])
    changed20 = metrics["changed_ratio_20"]["mean"]
    ssim_difference = max(0.0, 1.0 - metrics["ssim_luma"]["mean"])
    edge = metrics["edge_disagreement_ratio"]["mean"]
    temporal = min(20.0, metrics["effect_temporal_delta_255"]["mean"])
    visual_score = (
        mae * 1.5
        + changed20 * 35.0
        + ssim_difference * 30.0
        + edge * 15.0
        + temporal * 0.5
    )
    # Avoid allowing a nearly empty decode to win solely because it is different.
    survival_weight = 0.35 + 0.65 * np.sqrt(max(0.0, min(1.0, survival_ratio)))
    return float(visual_score * survival_weight)


def write_markdown(path: Path, report: dict) -> None:
    lines = [
        "# GLIC offline packet-glitch evaluation",
        "",
        (
            f"Compared {report['sample_count']} normalized timeline positions. "
            "Different decoded lengths are expected and do not abort this evaluator."
        ),
        "",
        "| Rank | Effect | Codec | Verdict | Survival | Frame loss | MAE | Pixels >=20 | SSIM | Temporal delta | Score | Keep |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for candidate in report["ranking"]:
        metrics = candidate["vs_control"]
        lines.append(
            "| {rank} | {effect} | {codec} | {verdict} | {survival:.1%} | "
            "{loss:.1%} | {mae:.2f} | {changed20:.1%} | {ssim:.4f} | "
            "{temporal:.2f} | {score:.2f} | {keep} |".format(
                rank=candidate["rank"],
                effect=candidate["effect"],
                codec=candidate["codec"],
                verdict=candidate["verdict"],
                survival=candidate["decode_survival_ratio"],
                loss=candidate["frame_loss_ratio"],
                mae=metrics["rgb_mae_255"]["mean"],
                changed20=metrics["changed_ratio_20"]["mean"],
                ssim=metrics["ssim_luma"]["mean"],
                temporal=metrics["effect_temporal_delta_255"]["mean"],
                score=candidate["ranking_score"],
                keep="yes" if candidate["offline_glitch_passed"] else "no",
            )
        )
    lines.extend(
        [
            "",
            "The keep gate accepts a visible spatial change, substantial frame loss, or a temporal delta, but rejects previews with fewer than 3% surviving frames.",
            "Ranking is quality-weighted by decode survival so an almost empty stream does not win merely by being maximally different.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    if args.samples < 2 or args.analysis_width < 1:
        raise ValueError("--samples must be >= 2 and --analysis-width must be positive")
    source = args.source.expanduser().resolve()
    control = args.control.expanduser().resolve()
    candidates = [parse_candidate(value) for value in args.candidate]
    paths = {"source": source, "control": control, **dict(candidates)}
    for label, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} video was not found: {path}")

    infos = {label: video_info(path) for label, path in paths.items()}
    captures = {label: cv2.VideoCapture(str(path)) for label, path in paths.items()}
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
    positions = np.linspace(0.0, 1.0, args.samples)
    try:
        for sample_index, position in enumerate(positions):
            source_frame = read_normalized_frame(
                captures["source"],
                int(infos["source"]["frames"]),
                float(position),
                args.analysis_width,
            )
            target_size = (source_frame.shape[1], source_frame.shape[0])
            frames = {"source": source_frame}
            for label in ("control", *(label for label, _ in candidates)):
                frames[label] = read_normalized_frame(
                    captures[label],
                    int(infos[label]["frames"]),
                    float(position),
                    args.analysis_width,
                    target_size,
                )
            if sample_index == args.samples // 2:
                representative_frames = {
                    label: frame.copy() for label, frame in frames.items()
                }

            metrics, effect = compare_frames(
                frames["source"],
                frames["control"],
                previous_effects["control_vs_source"],
            )
            for name, value in metrics.items():
                values["control_vs_source"][name].append(value)
            previous_effects["control_vs_source"] = effect

            for label, _ in candidates:
                for suffix, first in (
                    ("vs_source", frames["source"]),
                    ("vs_control", frames["control"]),
                ):
                    key = f"{label}_{suffix}"
                    metrics, effect = compare_frames(
                        first, frames[label], previous_effects[key]
                    )
                    for name, value in metrics.items():
                        values[key][name].append(value)
                    previous_effects[key] = effect
    finally:
        for capture in captures.values():
            capture.release()

    control_summary = summarize(values["control_vs_source"])
    candidate_reports = []
    for label, path in candidates:
        report = packet_report(path)
        vs_source = summarize(values[f"{label}_vs_source"])
        vs_control = summarize(values[f"{label}_vs_control"])
        verdict, visible = classify(vs_control)
        source_frames = int(
            report.get("source_frames", infos["control"]["frames"])
        )
        candidate_frames = int(
            report.get("salvaged_frames", infos[label]["frames"])
        )
        survival = float(
            report.get(
                "decode_survival_ratio",
                min(1.0, candidate_frames / max(1, source_frames)),
            )
        )
        frame_loss = max(0.0, 1.0 - survival)
        temporal_delta = vs_control["effect_temporal_delta_255"]["mean"]
        offline_passed = bool(
            survival >= 0.03
            and (visible or frame_loss >= 0.08 or temporal_delta >= 2.5)
            and report.get("qualified_preview", True)
        )
        candidate_reports.append(
            {
                "label": label,
                "path": str(path),
                "codec": report.get("codec", "unknown"),
                "effect": report.get("effect", label),
                "verdict": verdict,
                "spatial_glitch_passed": visible,
                "offline_glitch_passed": offline_passed,
                "decode_survival_ratio": survival,
                "frame_loss_ratio": frame_loss,
                "decoder_diagnostics": int(
                    report.get("decoder_diagnostics", 0)
                ),
                "source_frames": source_frames,
                "decoded_frames": candidate_frames,
                "vs_source": vs_source,
                "vs_control": vs_control,
                "ranking_score": ranking_score(vs_control, survival),
                "packet_report": str(path.with_suffix(path.suffix + ".json"))
                if report
                else None,
            }
        )

    ranking = sorted(
        candidate_reports,
        key=lambda candidate: (
            candidate["offline_glitch_passed"],
            candidate["ranking_score"],
        ),
        reverse=True,
    )
    for index, candidate in enumerate(ranking, start=1):
        candidate["rank"] = index

    report_data = {
        "schema": "glic-offline-packet-evaluation-v1",
        "source": str(source),
        "control": str(control),
        "analysis_width": representative_frames["source"].shape[1],
        "analysis_height": representative_frames["source"].shape[0],
        "sample_count": args.samples,
        "sampling": "normalized_timeline_positions",
        "codec_baseline_vs_source": control_summary,
        "gate": {
            "minimum_decode_survival_ratio": 0.03,
            "minimum_frame_loss_ratio": 0.08,
            "minimum_temporal_delta_255": 2.5,
            "spatial_verdicts": ["VISIBLE", "STRONG"],
        },
        "video_info": infos,
        "ranking": ranking,
    }

    rows = [
        cv2.hconcat(
            difference_panels(
                representative_frames["source"],
                representative_frames["control"],
                "CONTROL",
            )
        )
    ]
    for candidate in ranking:
        label = candidate["label"]
        rows.append(
            cv2.hconcat(
                difference_panels(
                    representative_frames["source"],
                    representative_frames[label],
                    (
                        f"{label} "
                        f"survival={candidate['decode_survival_ratio']:.0%}"
                    ),
                )
            )
        )
    heatmap = cv2.vconcat(rows)
    for path in (args.output_json, args.output_md, args.heatmap):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report_data, indent=2) + "\n")
    write_markdown(args.output_md, report_data)
    if not cv2.imwrite(str(args.heatmap), heatmap):
        raise RuntimeError(f"Failed to write heatmap: {args.heatmap}")

    for candidate in ranking:
        print(
            f"#{candidate['rank']} {candidate['label']}: "
            f"{candidate['verdict']} survival="
            f"{candidate['decode_survival_ratio']:.1%} "
            f"score={candidate['ranking_score']:.2f} "
            f"keep={candidate['offline_glitch_passed']}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
