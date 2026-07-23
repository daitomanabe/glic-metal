#!/usr/bin/env python3
"""Deterministic, token-free evolutionary search for offline codec glitches."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import cv2
import numpy as np

from process_codec_lab import EFFECTS


CODECS = ("h264", "hevc", "av1", "vp9", "prores")


def candidate(index: int, seed: int) -> dict:
    rng = np.random.default_rng(seed + index * 0x9E3779B1)
    effect = EFFECTS[index % len(EFFECTS)] if index < len(EFFECTS) else str(
        rng.choice(EFFECTS)
    )
    codec = str(rng.choice(CODECS))
    if effect == "av1_film_grain_instrument":
        codec = "av1"
    if effect == "av2_optical_flow_wound":
        codec = "av2"
    return {
        "index": index,
        "name": f"lab-{index:04d}-{effect}",
        "effect": effect,
        "codec": codec,
        "amount": round(float(rng.uniform(0.28, 0.98)), 6),
        "rate": round(float(rng.uniform(0.08, 0.96)), 6),
        "feedback": round(float(rng.uniform(0.12, 0.98)), 6),
        "seed": int(rng.integers(1, 2**31 - 1)),
    }


def read_frame(path: Path, position: float, width: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"could not open {path}")
        frames = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        capture.set(cv2.CAP_PROP_POS_FRAMES, round(position * (frames - 1)))
        success, frame = capture.read()
        if not success:
            raise RuntimeError(f"could not sample {path} at {position:.3f}")
    finally:
        capture.release()
    if frame.shape[1] > width:
        height = max(1, round(frame.shape[0] * width / frame.shape[1]))
        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    return frame


def measure(source: Path, output: Path, samples: int = 12) -> dict:
    maes = []
    changed = []
    edge = []
    temporal = []
    previous_difference = None
    for position in np.linspace(0.0, 1.0, samples):
        original = read_frame(source, float(position), 480)
        candidate_frame = read_frame(output, float(position), 480)
        if candidate_frame.shape[:2] != original.shape[:2]:
            candidate_frame = cv2.resize(
                candidate_frame,
                (original.shape[1], original.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
        difference = candidate_frame.astype(np.float32) - original.astype(
            np.float32
        )
        absolute = np.abs(difference)
        maes.append(float(absolute.mean()))
        changed.append(float((absolute.max(axis=2) >= 10.0).mean()))
        source_edges = cv2.Canny(
            cv2.cvtColor(original, cv2.COLOR_BGR2GRAY), 80, 160
        )
        output_edges = cv2.Canny(
            cv2.cvtColor(candidate_frame, cv2.COLOR_BGR2GRAY), 80, 160
        )
        union = np.logical_or(source_edges > 0, output_edges > 0)
        edge.append(
            float(
                np.logical_xor(source_edges > 0, output_edges > 0).sum()
                / max(1, union.sum())
            )
        )
        if previous_difference is not None:
            temporal.append(float(np.abs(difference - previous_difference).mean()))
        previous_difference = difference
    capture = cv2.VideoCapture(str(output))
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
    finally:
        capture.release()
    return {
        "rgb_mae_255": float(np.mean(maes)),
        "changed_ratio_10": float(np.mean(changed)),
        "edge_disagreement": float(np.mean(edge)),
        "effect_temporal_delta_255": float(np.mean(temporal)) if temporal else 0.0,
        "decoded_frames": frame_count,
        "fps": fps,
    }


def feature_vector(metrics: dict) -> np.ndarray:
    return np.asarray(
        [
            metrics["rgb_mae_255"] / 64.0,
            metrics["changed_ratio_10"],
            metrics["edge_disagreement"],
            metrics["effect_temporal_delta_255"] / 32.0,
        ],
        dtype=np.float64,
    )


def quality_score(metrics: dict) -> float:
    mae = metrics["rgb_mae_255"]
    changed = metrics["changed_ratio_10"]
    temporal = metrics["effect_temporal_delta_255"]
    visible = min(1.0, mae / 18.0) * 35.0
    coverage = (1.0 - min(1.0, abs(changed - 0.58) / 0.58)) * 30.0
    animation = min(1.0, temporal / 12.0) * 20.0
    structure = (1.0 - min(1.0, metrics["edge_disagreement"] / 0.92)) * 15.0
    return visible + coverage + animation + structure


def novelty(vector: np.ndarray, archive: list[dict]) -> float:
    if not archive:
        return 1.0
    distances = [
        float(np.linalg.norm(vector - np.asarray(item["feature_vector"])))
        for item in archive
    ]
    nearest = sorted(distances)[: min(5, len(distances))]
    return float(np.mean(nearest))


def write_ranking(output_dir: Path, archive: list[dict]) -> None:
    ranking = sorted(
        archive,
        key=lambda item: item["selection_score"],
        reverse=True,
    )
    for rank, item in enumerate(ranking, start=1):
        item["rank"] = rank
    payload = {
        "schema": "glic-evolutionary-codec-search-v1",
        "execution_class": "offline",
        "token_free": True,
        "ranking": ranking,
    }
    (output_dir / "ranking.json").write_text(json.dumps(payload, indent=2) + "\n")
    lines = [
        "# Evolutionary codec search ranking",
        "",
        "| Rank | Name | Effect | Codec | Quality | Novelty | Score | MAE | Changed |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in ranking:
        metrics = item["metrics"]
        lines.append(
            f"| {item['rank']} | {item['name']} | {item['effect']} | "
            f"{item['codec']} | {item['quality_score']:.2f} | "
            f"{item['novelty_score']:.3f} | {item['selection_score']:.2f} | "
            f"{metrics['rgb_mae_255']:.2f} | "
            f"{metrics['changed_ratio_10']:.1%} |"
        )
    (output_dir / "ranking.md").write_text("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("search-runs/codec-lab"))
    parser.add_argument("--donor", type=Path)
    parser.add_argument("--budget", type=int, default=64)
    parser.add_argument("--archive-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0x474C4943)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=270)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--max-frames", type=int, default=90)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    probes = [candidate(index, args.seed) for index in range(34)]
    assert len({item["name"] for item in probes}) == len(probes)
    assert set(EFFECTS).issubset({item["effect"] for item in probes})
    if args.selftest:
        print(
            f"PASS deterministic evolutionary search effects={len(EFFECTS)} "
            f"probe_candidates={len(probes)}"
        )
        return 0
    if args.input is None:
        raise ValueError("input is required unless --selftest is used")
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "search-state.json"
    archive: list[dict] = []
    start_index = 0
    if args.resume and state_path.is_file():
        state = json.loads(state_path.read_text())
        archive = state.get("archive", [])
        start_index = int(state.get("next_index", 0))

    for index in range(start_index, args.budget):
        recipe = candidate(index, args.seed)
        output = output_dir / f"{recipe['name']}.mp4"
        report = output.with_suffix(".mp4.json")
        command = [
            sys.executable,
            str(Path(__file__).with_name("process_codec_lab.py")),
            str(source),
            str(output),
            "--effect",
            recipe["effect"],
            "--codec",
            recipe["codec"],
            "--amount",
            str(recipe["amount"]),
            "--rate",
            str(recipe["rate"]),
            "--feedback",
            str(recipe["feedback"]),
            "--seed",
            str(recipe["seed"]),
            "--width",
            str(args.width),
            "--height",
            str(args.height),
            "--fps",
            str(args.fps),
            "--max-frames",
            str(args.max_frames),
            "--timeout",
            str(args.timeout),
            "--report",
            str(report),
        ]
        if args.donor:
            command.extend(["--donor", str(args.donor.expanduser().resolve())])
        started = time.monotonic()
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.timeout + 30,
            check=False,
        )
        if result.returncode == 0 and output.is_file():
            metrics = measure(source, output)
            vector = feature_vector(metrics)
            quality = quality_score(metrics)
            novelty_score = novelty(vector, archive)
            item = {
                **recipe,
                "output": str(output),
                "report": str(report),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "metrics": metrics,
                "feature_vector": vector.tolist(),
                "quality_score": quality,
                "novelty_score": novelty_score,
                "selection_score": quality + novelty_score * 28.0,
            }
            archive.append(item)
            archive = sorted(
                archive,
                key=lambda entry: entry["selection_score"],
                reverse=True,
            )[: args.archive_size]
        state_path.write_text(
            json.dumps(
                {
                    "schema": "glic-evolutionary-codec-search-state-v1",
                    "next_index": index + 1,
                    "budget": args.budget,
                    "seed": args.seed,
                    "archive": archive,
                    "last_return_code": result.returncode,
                    "last_stdout": result.stdout[-2000:],
                    "last_stderr": result.stderr[-2000:],
                },
                indent=2,
            )
            + "\n"
        )
        write_ranking(output_dir, archive)
        print(
            f"[{index + 1}/{args.budget}] {recipe['effect']} "
            f"status={result.returncode} archive={len(archive)}",
            flush=True,
        )
    return 0 if archive else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
