#!/usr/bin/env python3
"""Token-free batch rendering and diversity ranking for native syntax effects."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys

from native_syntax_glitch import EFFECTS
from process_native_syntax_glitch import (
    DEFAULT_CODEC,
    SUPPORTED_CODECS_BY_EFFECT,
    clean_x265_environment,
    normalized_encode_command,
    normalized_y4m_command,
    x265_entropy_command,
)
from process_offline_packet_glitch import preview_encoder_options, require_tool


CODECS = ("mpeg2", "mpeg4_part2", "h264", "hevc")
SCHEMA = "glic-native-syntax-ranking-v1"


def parse_amounts(value: str) -> list[float]:
    values: list[float] = []
    for item in value.split(","):
        try:
            amount = float(item)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"invalid amount: {item}"
            ) from error
        if not 0.0 < amount <= 1.0:
            raise argparse.ArgumentTypeError(
                "amounts must be greater than 0 and at most 1"
            )
        values.append(amount)
    if not values:
        raise argparse.ArgumentTypeError("at least one amount is required")
    return values


def parse_effects(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(values) - set(EFFECTS))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown effects: {', '.join(unknown)}"
        )
    return values


def run_logged(
    command: list[str],
    log: Path,
    *,
    environment: dict[str, str] | None = None,
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        output.write("+ " + shlex.join(command) + "\n")
        output.flush()
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
            env=environment,
        )
    return result.returncode


def source_name(codec: str) -> str:
    suffix = "y4m" if codec in ("h264", "hevc") else "avi"
    return f"source-{codec}.{suffix}"


def candidate_name(codec: str, effect: str, amount: float) -> str:
    amount_key = f"{amount:.3f}".rstrip("0").rstrip(".").replace(".", "p")
    return f"{codec}--{effect}--a{amount_key}"


def metric(candidate: dict, name: str) -> float:
    return float(candidate["vs_control"][name]["mean"])


def visual_fingerprint(candidate: dict) -> list[float]:
    return [
        min(metric(candidate, "rgb_mae_255") / 64.0, 1.0),
        metric(candidate, "changed_ratio_10"),
        metric(candidate, "changed_ratio_20"),
        1.0 - metric(candidate, "ssim_luma"),
        metric(candidate, "edge_disagreement_ratio"),
        min(metric(candidate, "effect_temporal_delta_255") / 64.0, 1.0),
    ]


def euclidean(left: list[float], right: list[float]) -> float:
    return math.sqrt(
        sum((left_value - right_value) ** 2 for left_value, right_value in zip(left, right))
        / len(left)
    )


def base_score(candidate: dict, survival: float) -> float:
    mae = metric(candidate, "rgb_mae_255")
    changed = metric(candidate, "changed_ratio_10")
    ssim = metric(candidate, "ssim_luma")
    edge = metric(candidate, "edge_disagreement_ratio")
    temporal = metric(candidate, "effect_temporal_delta_255")
    visibility = (
        min(mae / 32.0, 1.0) * 0.30
        + min(changed / 0.55, 1.0) * 0.25
        + min(max(1.0 - ssim, 0.0) / 0.65, 1.0) * 0.20
        + min(edge / 0.85, 1.0) * 0.10
        + min(temporal / 28.0, 1.0) * 0.15
    )
    excessive = max(0.0, (mae - 90.0) / 90.0) * 0.15
    return max(0.0, min(1.0, visibility * survival - excessive))


def rank_candidates(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    pending = [dict(row) for row in rows]
    for row in pending:
        row["visual_fingerprint"] = visual_fingerprint(row["difference"])
        row["base_score"] = round(
            base_score(row["difference"], row["decode_survival_ratio"]), 6
        )
    selected: list[dict] = []
    while pending:
        for row in pending:
            novelty = (
                1.0
                if not selected
                else min(
                    euclidean(
                        row["visual_fingerprint"],
                        chosen["visual_fingerprint"],
                    )
                    for chosen in selected
                )
            )
            row["novelty"] = round(novelty, 6)
            row["ranking_score"] = round(
                row["base_score"] * 0.72 + novelty * 0.28, 6
            )
        chosen = max(
            pending,
            key=lambda row: (
                row["ranking_score"],
                row["base_score"],
                row["name"],
            ),
        )
        chosen["rank"] = len(selected) + 1
        selected.append(chosen)
        pending.remove(chosen)
    return selected


def write_markdown(path: Path, report: dict) -> None:
    lines = [
        "# Native compressed-syntax ranking",
        "",
        (
            f"Candidates: {report['candidate_count']}; "
            f"qualified: {report['qualified_count']}; "
            f"failed runs: {len(report['failed_runs'])}."
        ),
        "",
        "| Rank | Candidate | Verdict | Score | Novelty | MAE | Pixels >=10 | SSIM | Survival |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["ranking"]:
        difference = row["difference"]
        lines.append(
            "| {rank} | `{name}` | {verdict} | {score:.3f} | "
            "{novelty:.3f} | {mae:.2f} | {changed:.1%} | {ssim:.4f} | "
            "{survival:.1%} |".format(
                rank=row["rank"],
                name=row["name"],
                verdict=difference["verdict"],
                score=row["ranking_score"],
                novelty=row["novelty"],
                mae=metric(difference, "rgb_mae_255"),
                changed=metric(difference, "changed_ratio_10"),
                ssim=metric(difference, "ssim_luma"),
                survival=row["decode_survival_ratio"],
            )
        )
    if report["failed_runs"]:
        lines.extend(["", "## Failed runs", ""])
        for failure in report["failed_runs"]:
            lines.append(
                f"- `{failure['name']}`: exit {failure['return_code']} "
                f"([{failure['log']}]({failure['log']}))"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def selftest() -> int:
    def fake(name: str, mae: float, changed: float, ssim: float, edge: float) -> dict:
        metrics = {
            "rgb_mae_255": {"mean": mae},
            "changed_ratio_10": {"mean": changed},
            "changed_ratio_20": {"mean": changed * 0.6},
            "ssim_luma": {"mean": ssim},
            "edge_disagreement_ratio": {"mean": edge},
            "effect_temporal_delta_255": {"mean": mae * 0.8},
        }
        return {
            "name": name,
            "decode_survival_ratio": 1.0,
            "difference": {
                "verdict": "VISIBLE",
                "meaningful_glitch_passed": True,
                "vs_control": metrics,
            },
        }

    rows = [
        fake("balanced", 28.0, 0.42, 0.63, 0.72),
        fake("similar", 27.0, 0.40, 0.65, 0.70),
        fake("different", 11.0, 0.18, 0.92, 0.15),
    ]
    first = rank_candidates(rows)
    second = rank_candidates(rows)
    assert first == second
    assert [row["rank"] for row in first] == [1, 2, 3]
    assert first[0]["base_score"] >= first[-1]["base_score"]
    print("PASS native syntax ranking and diversity selection")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render direct compressed-syntax candidates, measure actual-video "
            "difference, and create a deterministic diversity ranking."
        )
    )
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--codec",
        choices=("all",) + CODECS,
        default="all",
    )
    parser.add_argument("--effects", type=parse_effects, default=list(EFFECTS))
    parser.add_argument("--amounts", type=parse_amounts, default=[0.85])
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x474C4943)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=270)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--max-frames", type=int, default=45)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--ffedit", default=os.environ.get("GLIC_FFEDIT", "ffedit")
    )
    parser.add_argument(
        "--x264",
        default=os.environ.get("GLIC_X264_HOOK", "x264"),
    )
    parser.add_argument(
        "--h264-entropy",
        choices=("cabac", "cavlc", "both"),
        default="both",
        help="render one or both H.264 entropy-coder variants",
    )
    parser.add_argument(
        "--ffmpeg-hevc-decoder",
        default=os.environ.get(
            "GLIC_FFMPEG_HEVC_DECODER_HOOK",
            "ffmpeg-glic-hevc-decoder",
        ),
    )
    parser.add_argument(
        "--x265",
        default=os.environ.get(
            "GLIC_X265_HOOK", os.environ.get("GLIC_X265", "x265")
        ),
    )
    parser.add_argument(
        "--hevc-hook",
        choices=("auto", "entropy", "analysis"),
        default="auto",
    )
    parser.add_argument(
        "--hevc-lane",
        choices=("encoder", "decoder", "both"),
        default="both",
        help=(
            "render x265 late-entropy encoder hooks, existing-bitstream "
            "FFmpeg decoder hooks, or both"
        ),
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return args
    if args.input is None or args.output_dir is None:
        parser.error("input and --output-dir are required")
    args.input = args.input.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if min(args.width, args.height, args.fps, args.max_frames, args.threads) < 1:
        parser.error("dimensions, fps, max-frames, and threads must be positive")
    if args.width % 2 or args.height % 2:
        parser.error("--width and --height must be even")
    return args


def main() -> int:
    args = parse_args()
    if args.selftest:
        return selftest()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    selected_codecs = CODECS if args.codec == "all" else (args.codec,)
    ffedit = (
        require_tool(args.ffedit)
        if any(codec in ("mpeg2", "mpeg4_part2") for codec in selected_codecs)
        else args.ffedit
    )
    x264 = (
        require_tool(args.x264)
        if "h264" in selected_codecs
        else args.x264
    )
    x265 = (
        require_tool(args.x265)
        if "hevc" in selected_codecs
        else args.x265
    )
    ffmpeg_hevc_decoder = (
        require_tool(args.ffmpeg_hevc_decoder)
        if "hevc" in selected_codecs
        and args.hevc_lane in ("decoder", "both")
        else args.ffmpeg_hevc_decoder
    )
    script_directory = Path(__file__).resolve().parent
    root = script_directory.parent
    runner = script_directory / "process_native_syntax_glitch.py"
    evaluator = script_directory / "evaluate_effect_difference.py"
    if not evaluator.is_file():
        evaluator = root / "tools" / "evaluate_effect_difference.py"
    if not runner.is_file() or not evaluator.is_file():
        raise RuntimeError(
            "native syntax runner or difference evaluator is missing beside "
            "the installed batch tool"
        )
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    codecs = selected_codecs
    failed_runs: list[dict] = []
    ranking_rows: list[dict] = []

    for codec in codecs:
        codec_dir = output / codec
        codec_dir.mkdir(parents=True, exist_ok=True)
        source = codec_dir / source_name(codec)
        normalize_command = (
            normalized_y4m_command(
                ffmpeg,
                args.input,
                source,
                width=args.width,
                height=args.height,
                fps=args.fps,
                max_frames=args.max_frames,
                threads=args.threads,
            )
            if codec in ("h264", "hevc")
            else normalized_encode_command(
                ffmpeg,
                args.input,
                source,
                width=args.width,
                height=args.height,
                fps=args.fps,
                max_frames=args.max_frames,
                threads=args.threads,
                codec=codec,
            )
        )
        normalize_code = run_logged(
            normalize_command,
            codec_dir / "00-normalize.log",
        )
        if normalize_code != 0:
            raise RuntimeError(
                f"{codec} normalization failed; see {codec_dir / '00-normalize.log'}"
            )
        existing_hevc = None
        control_source = source
        if codec == "hevc":
            existing_hevc = codec_dir / "source-existing-hevc.hevc"
            existing_code = run_logged(
                x265_entropy_command(x265, source, existing_hevc),
                codec_dir / "00-existing-hevc.log",
                environment=clean_x265_environment(),
            )
            if existing_code != 0:
                raise RuntimeError(
                    "HEVC reference encode failed; see "
                    f"{codec_dir / '00-existing-hevc.log'}"
                )
            control_source = existing_hevc
        control = codec_dir / "control.mp4"
        control_code = run_logged(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(control_source),
                "-an",
                "-frames:v",
                str(args.max_frames),
                *preview_encoder_options(ffmpeg),
                "-movflags",
                "+faststart",
                str(control),
            ],
            codec_dir / "01-control.log",
        )
        if control_code != 0:
            raise RuntimeError(
                f"{codec} control encode failed; see {codec_dir / '01-control.log'}"
            )

        candidates: list[tuple[str, Path, Path]] = []
        for effect in args.effects:
            if codec not in SUPPORTED_CODECS_BY_EFFECT[effect]:
                continue
            for amount in args.amounts:
                variants: tuple[tuple[str, str, Path], ...]
                if codec == "h264":
                    entropy_modes = (
                        ("cabac", "cavlc")
                        if args.h264_entropy == "both"
                        else (args.h264_entropy,)
                    )
                    variants = tuple(
                        (mode, "normalize", source)
                        for mode in entropy_modes
                    )
                elif codec == "hevc":
                    assert existing_hevc is not None
                    lanes = (
                        ("encoder", "decoder")
                        if args.hevc_lane == "both"
                        else (args.hevc_lane,)
                    )
                    variants = tuple(
                        (
                            lane,
                            "normalize" if lane == "encoder" else "preserve",
                            source if lane == "encoder" else existing_hevc,
                        )
                        for lane in lanes
                    )
                else:
                    variants = (("not_applicable", "preserve", source),)
                for variant, source_mode, runner_source in variants:
                    name = candidate_name(codec, effect, amount)
                    if codec in ("h264", "hevc"):
                        name = f"{name}--{variant}"
                    preview = codec_dir / f"{name}.mp4"
                    report = codec_dir / f"{name}.report.json"
                    log = codec_dir / f"{name}.run.log"
                    reusable = False
                    if args.resume and preview.is_file() and report.is_file():
                        try:
                            prior = json.loads(
                                report.read_text(encoding="utf-8")
                            )
                            reusable = bool(prior.get("qualified_preview"))
                        except (OSError, json.JSONDecodeError):
                            reusable = False
                    if not reusable:
                        return_code = run_logged(
                            [
                                sys.executable,
                                str(runner),
                                str(runner_source),
                                str(preview),
                                "--codec",
                                codec,
                                "--source-mode",
                                source_mode,
                                "--effect",
                                effect,
                                "--amount",
                                str(amount),
                                "--seed",
                                str(args.seed),
                                "--fps",
                                str(args.fps),
                                "--max-frames",
                                str(args.max_frames),
                                "--threads",
                                str(args.threads),
                                "--width",
                                str(args.width),
                                "--height",
                                str(args.height),
                                "--ffmpeg",
                                ffmpeg,
                                "--ffprobe",
                                ffprobe,
                                "--ffedit",
                                ffedit,
                                "--x264",
                                x264,
                                "--h264-entropy",
                                (
                                    variant
                                    if codec == "h264"
                                    else "cabac"
                                ),
                                "--ffmpeg-hevc-decoder",
                                ffmpeg_hevc_decoder,
                                "--x265",
                                x265,
                                "--hevc-hook",
                                args.hevc_hook,
                                "--work-dir",
                                str(codec_dir / f"{name}-stages"),
                                "--report",
                                str(report),
                            ],
                            log,
                        )
                        if return_code != 0:
                            failed_runs.append(
                                {
                                    "name": name,
                                    "return_code": return_code,
                                    "log": str(log),
                                }
                            )
                            continue
                    candidates.append((name, preview, report))

        if not candidates:
            continue
        difference_json = codec_dir / "difference.json"
        difference_md = codec_dir / "difference.md"
        heatmap = codec_dir / "difference.png"
        difference_command = [
            sys.executable,
            str(evaluator),
            str(source),
            "--control",
            str(control),
        ]
        for name, preview, _ in candidates:
            difference_command.extend(["--candidate", f"{name}={preview}"])
        difference_command.extend(
            [
                "--analysis-width",
                str(args.width),
                "--output-json",
                str(difference_json),
                "--output-md",
                str(difference_md),
                "--heatmap",
                str(heatmap),
            ]
        )
        difference_code = run_logged(
            difference_command, codec_dir / "02-difference.log"
        )
        if difference_code != 0:
            raise RuntimeError(
                f"{codec} difference analysis failed; "
                f"see {codec_dir / '02-difference.log'}"
            )
        differences = {
            candidate["label"]: candidate
            for candidate in json.loads(
                difference_json.read_text(encoding="utf-8")
            )["candidates"]
        }
        for name, preview, report_path in candidates:
            native_report = json.loads(report_path.read_text(encoding="utf-8"))
            ranking_rows.append(
                {
                    "name": name,
                    "codec": codec,
                    "effect": native_report["effect"],
                    "amount": native_report["amount"],
                    "preview": str(preview),
                    "report": str(report_path),
                    "implementation_level": native_report[
                        "implementation_level"
                    ],
                    "decode_survival_ratio": native_report[
                        "decode_survival_ratio"
                    ],
                    "changed_syntax_values": native_report[
                        "mutation_evidence"
                    ]["changed_values"],
                    "difference": differences[name],
                }
            )

    ranking = rank_candidates(ranking_rows)
    report = {
        "schema": SCHEMA,
        "input": str(args.input),
        "codecs": list(codecs),
        "amounts": args.amounts,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "max_frames": args.max_frames,
        "candidate_count": len(ranking),
        "qualified_count": sum(
            row["difference"]["meaningful_glitch_passed"] for row in ranking
        ),
        "failed_runs": failed_runs,
        "ranking": ranking,
    }
    (output / "ranking.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(output / "ranking.md", report)
    print(
        f"candidates={len(ranking)} qualified={report['qualified_count']} "
        f"failed={len(failed_runs)} ranking={output / 'ranking.json'}"
    )
    if not ranking or (args.strict and failed_runs):
        return 4
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
