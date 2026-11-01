#!/usr/bin/env python3
"""Run one glitch gallery variant on a local video.

A recipe names one gallery variant and its exact parameters. Use --key with an
identifier such as original:vv01::saturation, or --recipe with the JSON saved
or copied from a gallery card. The family, effect, and codec always come from
the committed catalog, so a recipe cannot select an unknown processor.

The processors are the ones that rendered the gallery. The gallery delivery
stage mixes the processed video with its source by wet_mix; this script applies
the same mix, so a variant below 100% needs no separate ffmpeg step.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from typing import Any

import build_glitch_algorithm_gallery as gallery


DONOR_FAMILIES = {"codec_lab", "offline_packet", "structured"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--key", help="Gallery key, such as original:vv01::saturation.")
    source.add_argument(
        "--recipe", help="Recipe JSON from a gallery card; '-' reads stdin."
    )
    source.add_argument(
        "--list", action="store_true", help="Print every gallery key and exit."
    )
    parser.add_argument(
        "--family",
        choices=tuple(gallery.FAMILY_INFO),
        help="Limit --list to one family.",
    )
    parser.add_argument("--width", type=int, default=gallery.WIDTH)
    parser.add_argument("--height", type=int, default=gallery.HEIGHT)
    parser.add_argument("--fps", type=int, default=gallery.FPS)
    parser.add_argument(
        "--max-frames",
        type=int,
        help="Frame limit for offline families (default: the gallery limit).",
    )
    parser.add_argument(
        "--donor",
        type=Path,
        help="Second video for donor families (default: derived from the input).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Keep intermediate files here instead of a temporary directory.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Processor timeout in seconds (default: the gallery timeouts).",
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the processor command without running it.",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if not args.list:
        if not (args.key or args.recipe):
            parser.error("one of --key, --recipe, or --list is required")
        if args.input is None or args.output is None:
            parser.error("input and output are required")
    return args


def gallery_keys(catalog: dict[str, Any], family: str | None = None) -> list[str]:
    return [
        f"{algorithm['id']}::{preset['id']}"
        for algorithm in catalog["algorithms"]
        if family is None or algorithm["family"] == family
        for preset in algorithm["presets"]
    ]


def find_task(
    catalog: dict[str, Any], algorithm_id: str, variant_id: str
) -> gallery.RenderTask:
    for algorithm in catalog["algorithms"]:
        if algorithm["id"] != algorithm_id:
            continue
        for preset in algorithm["presets"]:
            if preset["id"] == variant_id:
                return gallery.RenderTask(algorithm, preset)
    raise ValueError(f"unknown gallery variant: {algorithm_id}::{variant_id}")


def task_from_key(catalog: dict[str, Any], key: str) -> gallery.RenderTask:
    algorithm_id, separator, variant_id = key.rpartition("::")
    if not separator:
        raise ValueError(f"gallery key must end in ::<variant>: {key}")
    return find_task(catalog, algorithm_id, variant_id)


def task_from_recipe(
    catalog: dict[str, Any], recipe: dict[str, Any]
) -> gallery.RenderTask:
    if recipe.get("schema") != gallery.RECIPE_SCHEMA:
        raise ValueError(f"recipe schema must be {gallery.RECIPE_SCHEMA}")
    task = find_task(
        catalog, str(recipe.get("algorithm_id")), str(recipe.get("variant_id"))
    )
    parameters = recipe.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != set(
        task.preset["parameters"]
    ):
        raise ValueError(f"recipe parameters do not match {task.key}")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in parameters.values()
    ):
        raise ValueError(f"recipe parameters must be numeric: {task.key}")
    return gallery.RenderTask(
        task.algorithm, {**task.preset, "parameters": parameters}
    )


def read_recipe(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text("utf-8")
    recipe = json.loads(text)
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be a JSON object")
    return recipe


def derive_donor(
    source: Path, destination: Path, args: argparse.Namespace, ffmpeg: str
) -> None:
    # reverse holds every decoded frame, so read only what the processor uses.
    seconds = (args.max_frames or gallery.MAX_FRAMES) / args.fps
    gallery.run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-t",
            str(seconds),
            "-i",
            str(source),
            "-vf",
            f"scale={args.width}:{args.height}:flags=lanczos,fps={args.fps},"
            f"format=yuv420p,{gallery.DONOR_FILTER}",
            "-af",
            "areverse",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        destination.with_suffix(".log"),
        args.timeout or 900,
    )


def deliver(
    task: gallery.RenderTask,
    source: Path,
    raw: Path,
    output: Path,
    args: argparse.Namespace,
    ffmpeg: str,
    log: Path,
) -> None:
    wet_mix = float(task.preset["parameters"]["wet_mix"])
    if wet_mix >= 1.0 and (
        gallery.processing_fps(task.algorithm, args.fps) == args.fps
    ):
        shutil.copyfile(raw, output)
        return
    gallery.run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-i",
            str(raw),
            "-filter_complex",
            gallery.wet_mix_filter(
                wet_mix, width=args.width, height=args.height, fps=args.fps
            ),
            "-map",
            "[out]",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "16",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        log,
        args.timeout or 900,
    )


def run(task: gallery.RenderTask, args: argparse.Namespace, work: Path) -> None:
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"input video not found: {source}")
    if output.exists() and not (args.overwrite or args.print_command):
        raise RuntimeError(f"output exists; pass --overwrite: {output}")

    family = task.algorithm["family"]
    dependencies = gallery.resolve_dependencies({"algorithms": [task.algorithm]})
    ffmpeg = dependencies["ffmpeg"]
    donor = (
        args.donor.expanduser().resolve() if args.donor else work / "donor.mp4"
    )
    raw = work / "raw.mp4"
    command = gallery.processor_command(
        task,
        source,
        donor,
        raw,
        work / "processor-report.json",
        work / "stages",
        dependencies,
        width=args.width,
        height=args.height,
        fps=args.fps,
        frame_limit=args.max_frames,
        processor_timeout=args.timeout,
    )
    if args.print_command:
        print(shlex.join(command))
        return

    if family in DONOR_FAMILIES and not args.donor:
        derive_donor(source, donor, args, ffmpeg)
    if "--max-frames" in command and not args.max_frames:
        print(
            f"note: {family} stops at the gallery frame limit; "
            "raise it with --max-frames",
            file=sys.stderr,
        )
    gallery.run_command(command, work / "processor.log", args.timeout or 3600)
    output.parent.mkdir(parents=True, exist_ok=True)
    deliver(task, source, raw, output, args, ffmpeg, work / "deliver.log")
    print(
        f"output={output} key={task.algorithm['id']}::{task.preset['id']} "
        f"wet_mix={task.preset['parameters']['wet_mix']}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = gallery.read_json(gallery.DEFAULT_CATALOG)
    if args.list:
        print("\n".join(gallery_keys(catalog, args.family)))
        return 0
    task = (
        task_from_key(catalog, args.key)
        if args.key
        else task_from_recipe(catalog, read_recipe(args.recipe))
    )
    if args.work_dir:
        work = args.work_dir.expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
        run(task, args, work)
        return 0
    work = Path(tempfile.mkdtemp(prefix="glic-recipe-"))
    try:
        run(task, args, work)
    except Exception:
        print(f"kept work directory: {work}", file=sys.stderr)
        raise
    shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
