#!/usr/bin/env python3
"""Write a provenance and hash manifest for an original-Metal validation run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Any


def command(*arguments: str) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(relative_to) if relative_to else path),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return payload


def benchmark_summary(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    results = payload.get("results", [])
    if not isinstance(results, list) or not results:
        raise RuntimeError(f"benchmark has no results: {path}")
    slowest_mean = max(results, key=lambda row: float(row["mean_ms"]))
    slowest_p95 = max(results, key=lambda row: float(row["p95_ms"]))
    return {
        "schema": payload.get("schema"),
        "backend": payload.get("backend"),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "warmup_frames": payload.get("warmup_frames"),
        "measured_frames": payload.get("frames"),
        "required_fps": payload.get("required_fps"),
        "supported_presets": payload.get("supported_presets"),
        "performance_passed": sum(
            1 for row in results if row.get("performance_passed") is True
        ),
        "slowest_mean": {
            key: slowest_mean.get(key)
            for key in ("preset", "mean_ms", "p95_ms", "fps", "mean_gpu_ms")
        },
        "slowest_p95": {
            key: slowest_p95.get(key)
            for key in ("preset", "mean_ms", "p95_ms", "fps")
        },
        "fidelity_lane": payload.get("fidelity_lane"),
        "cdf97_precision": payload.get("cdf97_precision"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    output_dir = args.output_dir.resolve()
    inputs: dict[str, Path] = {}
    for item in args.input:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            raise RuntimeError(f"invalid --input value: {item}")
        path = Path(value).resolve()
        if not path.is_file():
            raise RuntimeError(f"input does not exist: {path}")
        inputs[name] = path

    source_commit = command("git", "-C", str(repo_root), "rev-parse", "HEAD")
    source_status = command(
        "git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"
    )
    artifacts = []
    excluded = {"validation-manifest.json", "manifest.log"}
    for path in sorted(candidate for candidate in output_dir.rglob("*") if candidate.is_file()):
        if path.name not in excluded:
            artifacts.append(file_record(path, relative_to=output_dir))

    normal_path = output_dir / "benchmark-metal-normal.json"
    noise_path = output_dir / "benchmark-metal-noise.json"
    comparison = read_json(output_dir / "reference-comparison.json")
    video = read_json(next(output_dir.glob("*-original-metal-960x540-30fps.report.json")))
    qa = read_json(next(output_dir.glob("*-original-metal-960x540-30fps.qa.json")))
    video_filter = video.get("filter")
    if not isinstance(video_filter, dict):
        raise RuntimeError("video report is missing filter statistics")
    manifest = {
        "schema": "glic-original-metal-validation-manifest-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "repository": str(repo_root),
            "commit": source_commit,
            "tracked_worktree_clean": source_status == "",
            "tracked_status": source_status.splitlines(),
        },
        "machine": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "macos_version": command("sw_vers", "-productVersion"),
            "model": command("sysctl", "-n", "hw.model"),
            "cpu": command("sysctl", "-n", "machdep.cpu.brand_string"),
            "memory_bytes": int(command("sysctl", "-n", "hw.memsize")),
        },
        "inputs": {name: file_record(path) for name, path in sorted(inputs.items())},
        "summary": {
            "metal_normal": benchmark_summary(normal_path),
            "metal_noise": benchmark_summary(noise_path),
            "comparison": {
                key: comparison.get(key)
                for key in (
                    "preset_count",
                    "passed_count",
                    "failed_count",
                    "failed_presets",
                    "style_passed_count",
                    "style_failed_count",
                    "original_style_match_passed",
                )
            },
            "video": {
                key: video.get(key)
                for key in (
                    "target_width",
                    "target_height",
                    "target_fps",
                    "output_fps",
                    "end_to_end_observed_fps",
                    "end_to_end_average_30fps_passed",
                )
            }
            | {"processed_frames": video_filter.get("frames")},
            "video_qa": qa.get("summary"),
        },
        "artifacts": artifacts,
    }
    destination = output_dir / "validation-manifest.json"
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(destination)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}")
        raise SystemExit(1)
