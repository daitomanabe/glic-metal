#!/usr/bin/env python3
"""Aggregate repeated original_visual benchmarks and rank visible diversity.

The report is fail-closed: a preset is realtime-qualified only when every
supplied benchmark run contains valid certification evidence and passes both
mean and p95 at the requested frame budget.  Preview analysis is separate and
uses the same VISIBLE/clipping gates and morphology distance as the main
original-preset catalog.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from build_original_preset_catalog import (
    CatalogError,
    DEFAULT_UPSTREAM_PRESET_DIRECTORY,
    DEFAULT_UPSTREAM_PRESET_MANIFEST,
    decode_image,
    difference_metrics,
    extract_features,
    strict_json_load,
    sha256_file,
    verify_upstream_preset_corpus,
    visibility_score,
    visual_distance,
    visual_gate_reasons,
)


SCHEMA = "glic-original-visual-tier-analysis-v1"
BENCHMARK_SCHEMA = "glic-original-realtime-cpu-benchmark-v1"
MIN_WIDTH = 960
MIN_HEIGHT = 540
MIN_WARMUP = 10
MIN_FRAMES = 120
MIN_FPS = 30.0
MIN_REPEAT_RUNS = 3
MIN_SHORTLIST_DISTANCE = 0.10
EXPECTED_FIDELITY_LANE = (
    "upstream-colorspace-quadtree-fixed-predictor-quantize-"
    "reconstruct-exact-cdf97-no-serialization"
)

# Historical tier-one corpus. Everything else accepted by the current
# fail-closed original_visual lane is an exact CDF97 fixed-predictor preset.
NO_WAVELET_PRESETS = frozenset(
    {
        "0rg4n1c-___",
        "0rg4n1c-t1ny4ngl3z",
        "0rg4n1c-tr1angl3",
        "0rg4n1c-tr1f0rc3",
        "0rg4n1c-tr33",
        "0rg4n1c-v1n3z",
        "1amblu",
        "bi0g4n1c",
        "burn",
        "colour_glow",
        "default",
        "lightblur",
        "vv03",
        "vv07",
        "vv08",
        "vv10",
    }
)


class AnalysisError(RuntimeError):
    pass


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def load_benchmark(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        document = strict_json_load(path)
    except (CatalogError, OSError) as error:
        raise AnalysisError(f"cannot read benchmark {path}: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != BENCHMARK_SCHEMA:
        raise AnalysisError(f"unsupported benchmark schema: {path}")
    results = document.get("results")
    if not isinstance(results, list):
        raise AnalysisError(f"benchmark results are missing: {path}")
    by_name: dict[str, dict[str, Any]] = {}
    for row in results:
        if not isinstance(row, dict):
            raise AnalysisError(f"benchmark contains a non-object row: {path}")
        preset = row.get("preset")
        if not isinstance(preset, str) or not preset or preset in by_name:
            raise AnalysisError(f"benchmark contains an invalid/duplicate preset: {path}")
        by_name[preset] = row
    return document, by_name


def validate_benchmark_corpus(
    path: Path,
    root: dict[str, Any],
    supported: dict[str, dict[str, Any]],
    manifest_names: set[str],
) -> None:
    if root.get("fidelity_lane") != EXPECTED_FIDELITY_LANE:
        raise AnalysisError(f"benchmark fidelity lane is not exact CDF97: {path}")
    if root.get("unsupported_policy") != "fail-closed":
        raise AnalysisError(f"benchmark unsupported policy is not fail-closed: {path}")
    for field in ("scanned_presets", "decoded_presets"):
        if root.get(field) != len(manifest_names):
            raise AnalysisError(f"benchmark {field} is not the pinned corpus size: {path}")
    if root.get("load_failures") != 0 or root.get("load_failure_presets") not in ([], None):
        raise AnalysisError(f"benchmark has preset load failures: {path}")
    if root.get("supported_presets") != len(supported):
        raise AnalysisError(f"benchmark supported count disagrees with results: {path}")
    unsupported_rows = root.get("unsupported_results")
    if not isinstance(unsupported_rows, list):
        raise AnalysisError(f"benchmark unsupported results are missing: {path}")
    unsupported_names: set[str] = set()
    for row in unsupported_rows:
        if not isinstance(row, dict) or not isinstance(row.get("preset"), str):
            raise AnalysisError(f"benchmark has invalid unsupported row: {path}")
        name = row["preset"]
        if name in unsupported_names:
            raise AnalysisError(f"benchmark has duplicate unsupported preset: {path}")
        unsupported_names.add(name)
    if root.get("unsupported_presets") != len(unsupported_names):
        raise AnalysisError(f"benchmark unsupported count disagrees with rows: {path}")
    supported_names = set(supported)
    if supported_names & unsupported_names:
        raise AnalysisError(f"benchmark supported and unsupported sets overlap: {path}")
    if supported_names | unsupported_names != manifest_names:
        raise AnalysisError(f"benchmark does not cover the pinned 144-name corpus: {path}")


def performance_gate(root: dict[str, Any], row: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    fps = _finite(root.get("required_fps"))
    width = _finite(root.get("width"))
    height = _finite(root.get("height"))
    warmup = _finite(root.get("warmup_frames"))
    frames = _finite(root.get("frames"))
    if root.get("backend") != "cpu-reference":
        reasons.append("unexpected_backend")
    if width is None or width < MIN_WIDTH or height is None or height < MIN_HEIGHT:
        reasons.append("resolution_below_certification_minimum")
    if warmup is None or warmup < MIN_WARMUP:
        reasons.append("insufficient_warmup_frames")
    if frames is None or frames < MIN_FRAMES:
        reasons.append("insufficient_measured_frames")
    if fps is None or fps < MIN_FPS:
        reasons.append("required_fps_below_minimum")
    if root.get("certification_evidence_passed") is not True:
        reasons.append("certification_evidence_not_passed")

    mean_ms = _finite(row.get("mean_ms"))
    p95_ms = _finite(row.get("p95_ms"))
    budget = 1000.0 / (fps if fps is not None and fps > 0 else MIN_FPS)
    if mean_ms is None or mean_ms > budget:
        reasons.append("mean_exceeds_frame_budget")
    if p95_ms is None or p95_ms > budget:
        reasons.append("p95_exceeds_frame_budget")
    for field in ("process_passed", "timing_passed", "performance_passed"):
        if row.get(field) is not True:
            reasons.append(f"{field}_is_not_true")
    return not reasons, reasons


def _tier(preset: str) -> str:
    return (
        "no_wavelet_fixed_predictor"
        if preset in NO_WAVELET_PRESETS
        else "cdf97_fixed_predictor"
    )


def build_report(
    benchmark_paths: list[Path],
    dry_path: Path,
    preview_directory: Path,
    shortlist_count: int,
    *,
    manifest_path: Path | None = DEFAULT_UPSTREAM_PRESET_MANIFEST,
    corpus_directory: Path | None = DEFAULT_UPSTREAM_PRESET_DIRECTORY,
    validate_full_corpus: bool = True,
) -> dict[str, Any]:
    if len(benchmark_paths) < 1:
        raise AnalysisError("at least one benchmark is required")
    if shortlist_count < 1:
        raise AnalysisError("shortlist count must be positive")
    loaded = [load_benchmark(path) for path in benchmark_paths]
    if validate_full_corpus:
        try:
            provenance = verify_upstream_preset_corpus(
                manifest_path, corpus_directory
            )
        except CatalogError as error:
            raise AnalysisError(str(error)) from error
        if provenance.get("verified") is not True:
            raise AnalysisError(
                "pinned upstream preset corpus verification failed: "
                + ",".join(provenance.get("reasons", []))
            )
        manifest_names = set(provenance["manifest_names"])
        for path, (root, supported) in zip(benchmark_paths, loaded):
            validate_benchmark_corpus(path, root, supported, manifest_names)
    else:
        provenance = {
            "verified": False,
            "reasons": ["full_corpus_validation_disabled_for_test_fixture"],
            "manifest_names": [],
            "upstream_commit": None,
            "manifest": None,
            "directory": None,
        }
    name_sets = [set(rows) for _, rows in loaded]
    if any(names != name_sets[0] for names in name_sets[1:]):
        raise AnalysisError("benchmark preset name sets do not match")
    dry = decode_image(dry_path, "dry image")
    if dry.shape[:2] != (MIN_HEIGHT, MIN_WIDTH):
        raise AnalysisError("dry image must be exactly 960x540")
    if validate_full_corpus:
        preview_names = {
            item.stem for item in preview_directory.iterdir() if item.suffix == ".png"
        }
        if preview_names != name_sets[0]:
            raise AnalysisError(
                "preview PNG name set does not exactly match supported benchmark rows"
            )

    rows: list[dict[str, Any]] = []
    for preset in sorted(name_sets[0], key=lambda value: (value.casefold(), value)):
        runs: list[dict[str, Any]] = []
        for path, (root, by_name) in zip(benchmark_paths, loaded):
            source = by_name[preset]
            passed, reasons = performance_gate(root, source)
            runs.append(
                {
                    "benchmark": str(path),
                    "passed": passed,
                    "reasons": reasons,
                    "mean_ms": source.get("mean_ms"),
                    "p95_ms": source.get("p95_ms"),
                    "fps": source.get("fps"),
                }
            )
        repeat_consensus = all(run["passed"] for run in runs)

        preview_path = preview_directory / f"{preset}.png"
        wet = decode_image(preview_path, f"preview for {preset}")
        if wet.shape != dry.shape:
            raise AnalysisError(f"preview dimensions differ for {preset}")
        metrics = difference_metrics(dry, wet)
        perceptual = extract_features(wet, dry)
        visual_reasons = visual_gate_reasons(metrics)
        rows.append(
            {
                "preset": preset,
                "tier": _tier(preset),
                "repeat_consensus_passed": repeat_consensus,
                "visual_integrity_passed": not visual_reasons,
                "combined_realtime_visible_passed": repeat_consensus
                and not visual_reasons,
                "performance_runs": runs,
                "worst_mean_ms": max(float(run["mean_ms"]) for run in runs),
                "worst_p95_ms": max(float(run["p95_ms"]) for run in runs),
                "minimum_fps": min(float(run["fps"]) for run in runs),
                "visual_gate_reasons": visual_reasons,
                "metrics": metrics,
                "effect_presence_score": visibility_score(metrics, perceptual),
                "perceptual": perceptual,
                "preview": str(preview_path),
                "preview_sha256": sha256_file(preview_path),
            }
        )

    eligible = [dict(row) for row in rows if row["combined_realtime_visible_passed"]]
    shortlist: list[dict[str, Any]] = []
    selected_descriptors: list[dict[str, Any]] = []
    remaining = {row["preset"]: row for row in eligible}
    while remaining and len(shortlist) < shortlist_count:
        scored: list[tuple[float, float, str, dict[str, Any]]] = []
        for preset, row in remaining.items():
            if selected_descriptors:
                distance = min(
                    visual_distance(row["perceptual"], descriptor)
                    for descriptor in selected_descriptors
                )
                if distance < MIN_SHORTLIST_DISTANCE:
                    continue
                score = 0.90 * distance + 0.10 * float(
                    row["effect_presence_score"]
                )
            else:
                distance = 1.0
                score = float(row["effect_presence_score"])
            scored.append((score, distance, preset, row))
        if not scored:
            break
        _, distance, preset, row = min(
            scored,
            key=lambda item: (-item[0], -item[1], item[2].casefold(), item[2]),
        )
        rank = len(shortlist) + 1
        shortlist.append(
            {
                "rank": rank,
                "preset": row["preset"],
                "tier": row["tier"],
                "effect_presence_score": row["effect_presence_score"],
                "minimum_distance_to_earlier": None
                if not selected_descriptors
                else round(distance, 8),
                "worst_mean_ms": row["worst_mean_ms"],
                "worst_p95_ms": row["worst_p95_ms"],
                "minimum_fps": row["minimum_fps"],
                "preview": row["preview"],
                "mae_rgb": row["metrics"]["mae_rgb"],
                "changed_pixel_ratio": row["metrics"]["changed_pixel_ratio"],
                "windowed_luma_ssim": row["metrics"]["windowed_luma_ssim"],
                "highlight_clipping_ratio": row["metrics"][
                    "highlight_clipping_ratio"
                ],
            }
        )
        selected_descriptors.append(row["perceptual"])
        del remaining[preset]

    benchmark_evidence = [
        {"path": str(path.resolve()), "sha256": sha256_file(path)}
        for path in benchmark_paths
    ]
    distinct_run_evidence = (
        len({entry["path"] for entry in benchmark_evidence})
        == len(benchmark_evidence)
        and len({entry["sha256"] for entry in benchmark_evidence})
        == len(benchmark_evidence)
    )
    shortlist_quota_satisfied = len(shortlist) == shortlist_count
    return {
        "schema": SCHEMA,
        "fidelity_lane": "original_visual_algorithmic_core",
        "approximation_substitution": False,
        "publishable": bool(
            provenance.get("verified") is True
            and len(benchmark_paths) >= MIN_REPEAT_RUNS
            and distinct_run_evidence
            and bool(eligible)
            and shortlist_quota_satisfied
        ),
        "upstream_provenance": provenance,
        "benchmark_runs": benchmark_evidence,
        "distinct_repeat_evidence": distinct_run_evidence,
        "dry_image": {"path": str(dry_path), "sha256": sha256_file(dry_path)},
        "preview_directory": str(preview_directory),
        "gates": {
            "minimum_resolution": "960x540",
            "minimum_warmup_frames": MIN_WARMUP,
            "minimum_measured_frames": MIN_FRAMES,
            "minimum_required_fps": MIN_FPS,
            "repeat_policy": "every supplied run must pass mean and p95",
            "minimum_repeat_runs_for_publishable": MIN_REPEAT_RUNS,
            "minimum_mae_rgb": 10.0,
            "minimum_changed_pixel_ratio": 0.25,
            "maximum_windowed_luma_ssim": 0.95,
            "maximum_highlight_clipping_ratio": 0.15,
            "minimum_shortlist_morphology_distance": MIN_SHORTLIST_DISTANCE,
            "requested_shortlist_count": shortlist_count,
        },
        "counts": {
            "supported": len(rows),
            "repeat_consensus_passed": sum(
                row["repeat_consensus_passed"] for row in rows
            ),
            "visual_integrity_passed": sum(
                row["visual_integrity_passed"] for row in rows
            ),
            "combined_realtime_visible_passed": sum(
                row["combined_realtime_visible_passed"] for row in rows
            ),
            "cdf97_supported": sum(
                row["tier"] == "cdf97_fixed_predictor" for row in rows
            ),
            "cdf97_repeat_consensus_passed": sum(
                row["tier"] == "cdf97_fixed_predictor"
                and row["repeat_consensus_passed"]
                for row in rows
            ),
            "cdf97_combined_realtime_visible_passed": sum(
                row["tier"] == "cdf97_fixed_predictor"
                and row["combined_realtime_visible_passed"]
                for row in rows
            ),
            "shortlist_selected": len(shortlist),
            "shortlist_quota_satisfied": shortlist_quota_satisfied,
        },
        "performance_failures": [
            row["preset"] for row in rows if not row["repeat_consensus_passed"]
        ],
        "visual_failures": [
            {"preset": row["preset"], "reasons": row["visual_gate_reasons"]}
            for row in rows
            if not row["visual_integrity_passed"]
        ],
        "diversity_shortlist": shortlist,
        "rows": rows,
    }


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_html(report: dict[str, Any]) -> str:
    counts = report["counts"]
    preview_folder = Path(report["preview_directory"]).name
    cards = "".join(
        "<article><img loading='lazy' src='{}' alt='{}'><h2>{}</h2>"
        "<p>p95 worst {:.3f} ms · MAE {:.2f} · distance {}</p></article>".format(
            html.escape(str(Path(preview_folder) / (row["preset"] + ".png"))),
            html.escape(row["preset"]),
            html.escape(row["preset"]),
            row["worst_p95_ms"],
            row["mae_rgb"],
            "first"
            if row["minimum_distance_to_earlier"] is None
            else f"{row['minimum_distance_to_earlier']:.3f}",
        )
        for row in report["diversity_shortlist"]
    )
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>GLIC original_visual repeat consensus</title>
<style>body{{font:15px system-ui;margin:2rem;background:#111;color:#eee}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}}article{{background:#1d1d1d;padding:1rem;border-radius:10px}}img{{width:100%;height:auto}}h1,h2{{font-weight:600}}</style>
<h1>original_visual repeat consensus</h1>
<p>{counts['repeat_consensus_passed']}/{counts['supported']} realtime consensus · {counts['combined_realtime_visible_passed']} realtime + visible · no approximation substitution</p>
<main>{cards}</main>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="append", required=True, type=Path)
    parser.add_argument("--dry", required=True, type=Path)
    parser.add_argument("--preview-dir", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--html", type=Path)
    parser.add_argument("--shortlist-count", type=int, default=8)
    parser.add_argument(
        "--preset-manifest", type=Path, default=DEFAULT_UPSTREAM_PRESET_MANIFEST
    )
    parser.add_argument(
        "--preset-corpus", type=Path, default=DEFAULT_UPSTREAM_PRESET_DIRECTORY
    )
    args = parser.parse_args(argv)
    if args.shortlist_count < 1:
        parser.error("--shortlist-count must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = build_report(
            args.benchmark,
            args.dry,
            args.preview_dir,
            args.shortlist_count,
            manifest_path=args.preset_manifest,
            corpus_directory=args.preset_corpus,
        )
        write_atomic(args.json, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.html:
            write_atomic(args.html, render_html(report))
    except (AnalysisError, CatalogError, ValueError, KeyError) as error:
        print(f"analysis failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
