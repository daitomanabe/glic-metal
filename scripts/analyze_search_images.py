#!/usr/bin/env python3
"""Build a cached, reproducible image-analysis sidecar for search elites.

The visual-liveliness implementation remains an external instrument.  This
adapter proves that instrument with its self-test, validates every requested
row (the upstream CLI intentionally continues after per-file failures), and
atomically publishes only complete snapshots.  A complementary repository
analyzer supplies perceptual descriptors used for near-duplicate clustering.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from build_search_catalog import archive_records, candidate_identity


SCHEMA = "glic-search-image-analysis-v1"
LIVELINESS_FIELDS = (
    "lum_mean",
    "occ_soft",
    "occ_hard",
    "bg",
    "blobs",
    "area_cv",
    "elong_mean",
    "elong_cv",
    "solidity",
    "shape_entropy",
    "flow_mag",
    "dir_entropy",
    "vorticity",
    "moving_frac",
    "camera_flow",
)
PERCEPTUAL_FIELDS = (
    "colorfulness",
    "saturation_mean",
    "saturation_std",
    "local_contrast",
    "edge_density",
    "blockiness",
    "channel_separation",
    "phash",
    "dhash",
    "hsv_hist",
    "luma_grid",
    "edge_grid",
    "color_grid",
    "residual_reference",
    "residual_mask_coverage",
    "residual_blockiness",
    "dominant_artifact_scale_px",
    "dominant_artifact_scale_fraction",
    "artifact_scale_bucket",
    "artifact_orientation",
    "residual_horizontal_energy",
    "residual_vertical_energy",
    "residual_phash",
    "residual_dhash",
    "residual_luma_grid",
    "residual_edge_grid",
    "residual_blockiness_multiscale",
    "residual_scale_histogram",
    "residual_orientation",
)
PERCEPTUAL_SCALAR_FIELDS = PERCEPTUAL_FIELDS[:7]
MORPHOLOGY_SCALAR_FIELDS = (
    "residual_mask_coverage",
    "residual_blockiness",
    "dominant_artifact_scale_px",
    "dominant_artifact_scale_fraction",
    "residual_horizontal_energy",
    "residual_vertical_energy",
)
PERCEPTUAL_VECTOR_LENGTHS = {
    "hsv_hist": 128,
    "luma_grid": 64,
    "edge_grid": 64,
    "color_grid": 48,
    "residual_luma_grid": 64,
    "residual_edge_grid": 64,
    "residual_blockiness_multiscale": 7,
    "residual_scale_histogram": 7,
    "residual_orientation": 2,
}


class AnalysisError(RuntimeError):
    pass


ACTIVE_PROCESS: subprocess.Popen[str] | None = None


def terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()


def handle_process_signal(signum: int, _frame: Any) -> None:
    process = ACTIVE_PROCESS
    if process is not None:
        terminate_process_group(process)
    raise SystemExit(128 + signum)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def safe_preview_path(run_dir: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise AnalysisError("archive elite has no preview path")
    path = Path(raw_path).expanduser()
    resolved = path.resolve() if path.is_absolute() else (run_dir / path).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as error:
        raise AnalysisError(f"preview escapes run directory: {raw_path}") from error
    if resolved.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise AnalysisError(f"unsupported preview image: {raw_path}")
    return resolved


def resolve_dry_reference(archive: dict[str, Any], run_dir: Path) -> Path | None:
    inputs = archive.get("inputs")
    if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], str):
        return None
    raw = Path(inputs[0]).expanduser()
    if raw.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    if raw.is_absolute():
        resolved = raw.resolve()
        return resolved if resolved.is_file() else None

    # Current runs record repo-relative inputs while older copied runs may
    # contain a path relative to the run directory.  Resolve only beneath the
    # two explicit roots; do not let an archive traverse elsewhere.
    repo_root = Path(__file__).resolve().parents[1]
    for base in (run_dir, repo_root):
        resolved = (base / raw).resolve()
        try:
            resolved.relative_to(base.resolve())
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def analysis_key(preview_sha256: str, reference_sha256: str) -> str:
    return hashlib.sha256(
        f"{preview_sha256}\0{reference_sha256 or 'wet-only'}".encode("utf-8")
    ).hexdigest()


def load_targets(
    run_dir: Path, archive_path: Path
) -> tuple[list[dict[str, str]], str, bytes, Path | None]:
    archive_bytes = archive_path.read_bytes()
    try:
        archive = json.loads(archive_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalysisError(f"archive is not readable: {error}") from error
    records = archive_records(archive)
    dry_reference = resolve_dry_reference(archive, run_dir)
    reference_sha256 = sha256_file(dry_reference) if dry_reference is not None else ""
    targets: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    seen_recipes: set[str] = set()
    for index, record in enumerate(records):
        candidate_id = candidate_identity(record, f"archive-{index + 1:08d}")
        recipe_hash = str(record.get("recipe_hash") or record.get("canonical_hash") or "")
        if not recipe_hash:
            raise AnalysisError(f"candidate {candidate_id} has no recipe hash")
        if recipe_hash in seen_recipes:
            continue
        seen_recipes.add(recipe_hash)
        label = f"candidate-{candidate_id}-{recipe_hash}"
        if "|" in label or label in seen_labels:
            raise AnalysisError(f"candidate label is not unique/safe: {label}")
        seen_labels.add(label)
        raw_preview = record.get("preview") or record.get("preview_path")
        preview = safe_preview_path(run_dir, raw_preview)
        if not preview.is_file():
            raise AnalysisError(f"preview disappeared while reading archive: {preview}")
        targets.append(
            {
                "candidate_id": str(candidate_id),
                "recipe_hash": recipe_hash,
                "label": label,
                "preview_path": str(preview.relative_to(run_dir)),
                "absolute_path": str(preview),
                "preview_sha256": sha256_file(preview),
                "reference_absolute_path": ""
                if dry_reference is None
                else str(dry_reference),
                "reference_sha256": reference_sha256,
            }
        )
        targets[-1]["analysis_key_sha256"] = analysis_key(
            targets[-1]["preview_sha256"], reference_sha256
        )
    targets.sort(key=lambda item: (item["recipe_hash"], item["candidate_id"]))
    return targets, hashlib.sha256(archive_bytes).hexdigest(), archive_bytes, dry_reference


def cache_preview(
    target: dict[str, str], run_dir: Path, cache_dir: Path, temporary_dir: Path
) -> dict[str, str]:
    stable = dict(target)
    suffix = Path(target["absolute_path"]).suffix.lower()
    cached_path = cache_dir / f'{target["preview_sha256"]}{suffix}'
    if not cached_path.is_file() or sha256_file(cached_path) != target["preview_sha256"]:
        temporary_path = temporary_dir / f'{target["preview_sha256"]}{suffix}'
        try:
            shutil.copyfile(target["absolute_path"], temporary_path)
        except OSError as error:
            raise AnalysisError(
                f"preview changed while taking the analysis snapshot: {target['absolute_path']}"
            ) from error
        if sha256_file(temporary_path) != target["preview_sha256"]:
            raise AnalysisError(f"preview content changed during snapshot: {target['absolute_path']}")
        os.replace(temporary_path, cached_path)
    stable["absolute_path"] = str(cached_path)
    stable["preview_path"] = str(cached_path.relative_to(run_dir))
    reference_path = target.get("reference_absolute_path")
    reference_sha256 = target.get("reference_sha256")
    if reference_path and reference_sha256:
        source_reference = Path(reference_path)
        reference_suffix = source_reference.suffix.lower()
        cached_reference = cache_dir / f"dry-{reference_sha256}{reference_suffix}"
        if not cached_reference.is_file() or sha256_file(cached_reference) != reference_sha256:
            temporary_reference = temporary_dir / f"dry-{reference_sha256}{reference_suffix}"
            try:
                shutil.copyfile(source_reference, temporary_reference)
            except OSError as error:
                raise AnalysisError(
                    f"dry reference changed while taking the analysis snapshot: {source_reference}"
                ) from error
            if sha256_file(temporary_reference) != reference_sha256:
                raise AnalysisError(
                    f"dry reference content changed during snapshot: {source_reference}"
                )
            os.replace(temporary_reference, cached_reference)
        stable["reference_absolute_path"] = str(cached_reference)
        stable["reference_path"] = str(cached_reference.relative_to(run_dir))
    else:
        stable["reference_path"] = ""
    return stable


def command_prefix(path: Path) -> list[str]:
    if os.access(path, os.X_OK):
        return [str(path)]
    return ["/bin/bash", str(path)]


def run_checked(command: list[str], description: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    global ACTIVE_PROCESS
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        ACTIVE_PROCESS = process
        stdout, stderr = process.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        if ACTIVE_PROCESS is not None:
            terminate_process_group(ACTIVE_PROCESS)
        raise AnalysisError(f"{description} exceeded {timeout} seconds") from error
    except OSError as error:
        raise AnalysisError(f"{description} failed to run: {error}") from error
    finally:
        ACTIVE_PROCESS = None
    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr).strip()
        raise AnalysisError(f"{description} failed with exit {result.returncode}: {output[-2000:]}")
    return result


def run_selftests(runner: Path, feature_python: Path, feature_script: Path) -> None:
    result = run_checked(command_prefix(runner) + ["--selftest"], "visual-liveliness self-test", 120)
    if "SELFTEST PASSED" not in result.stdout:
        raise AnalysisError("visual-liveliness self-test did not report SELFTEST PASSED")
    if not feature_python.is_file():
        raise AnalysisError(
            f"managed visual-liveliness Python was not created by the self-test: {feature_python}"
        )
    result = run_checked(
        [str(feature_python), str(feature_script), "--selftest"],
        "perceptual feature self-test",
        120,
    )
    if "SELFTEST PASSED" not in result.stdout:
        raise AnalysisError("perceptual feature self-test did not report SELFTEST PASSED")


def read_json_rows(path: Path, description: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalysisError(f"{description} returned invalid JSON: {error}") from error
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise AnalysisError(f"{description} JSON must be an array of objects")
    return payload


def index_validated_rows(
    rows: list[dict[str, Any]], expected_labels: set[str], required_fields: Iterable[str], description: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = row.get("name")
        if not isinstance(label, str) or label not in expected_labels or label in indexed:
            raise AnalysisError(f"{description} returned an unknown or duplicate label: {label!r}")
        for field in required_fields:
            if field not in row:
                raise AnalysisError(f"{description} omitted {field} for {label}")
            value = row[field]
            if isinstance(value, list):
                if not value or not all(finite_number(item) for item in value):
                    raise AnalysisError(f"{description} returned invalid vector {field} for {label}")
            elif not finite_number(value) and not isinstance(value, str):
                raise AnalysisError(f"{description} returned non-finite {field} for {label}")
        indexed[label] = row
    missing = sorted(expected_labels - indexed.keys())
    if missing:
        raise AnalysisError(f"{description} silently omitted {len(missing)} row(s): {missing[:3]}")
    return indexed


def validate_perceptual_rows(rows: dict[str, dict[str, Any]]) -> None:
    for label, row in rows.items():
        for name in ("phash", "dhash", "residual_phash", "residual_dhash"):
            value = row.get(name)
            if not isinstance(value, str) or len(value) != 16:
                raise AnalysisError(f"perceptual feature analyzer returned invalid {name} for {label}")
            try:
                int(value, 16)
            except ValueError as error:
                raise AnalysisError(
                    f"perceptual feature analyzer returned non-hex {name} for {label}"
                ) from error
        for name, length in PERCEPTUAL_VECTOR_LENGTHS.items():
            value = row.get(name)
            if not isinstance(value, list) or len(value) != length:
                raise AnalysisError(
                    f"perceptual feature analyzer returned invalid {name} length for {label}"
                )
        if row.get("residual_reference") not in ("dry_wet", "unavailable"):
            raise AnalysisError(
                f"perceptual feature analyzer returned invalid residual reference for {label}"
            )
        if row.get("artifact_scale_bucket") not in (
            "none",
            "micro",
            "fine",
            "medium",
            "coarse",
            "mega",
        ):
            raise AnalysisError(
                f"perceptual feature analyzer returned invalid scale bucket for {label}"
            )
        if row.get("artifact_orientation") not in (
            "none",
            "horizontal",
            "vertical",
            "bidirectional",
        ):
            raise AnalysisError(
                f"perceptual feature analyzer returned invalid orientation for {label}"
            )


def analyze_batch(
    targets: list[dict[str, str]],
    runner: Path,
    feature_python: Path,
    feature_script: Path,
    temporary_dir: Path,
    batch_index: int,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    labels = {target["label"] for target in targets}
    liveliness_path = temporary_dir / f"liveliness-{batch_index:04d}.json"
    feature_path = temporary_dir / f"perceptual-{batch_index:04d}.json"
    liveliness_specs = [f'{target["absolute_path"]}|1|{target["label"]}' for target in targets]
    feature_specs = [
        f'{target["absolute_path"]}|0|{target["label"]}'
        + (
            f'|{target["reference_absolute_path"]}'
            if target.get("reference_absolute_path")
            else ""
        )
        for target in targets
    ]

    run_checked(
        command_prefix(runner) + ["--json", str(liveliness_path), *liveliness_specs],
        "visual-liveliness batch",
    )
    run_checked(
        [str(feature_python), str(feature_script), "--json", str(feature_path), *feature_specs],
        "perceptual feature batch",
    )
    liveliness = index_validated_rows(
        read_json_rows(liveliness_path, "visual-liveliness"), labels, LIVELINESS_FIELDS, "visual-liveliness"
    )
    feature_rows = read_json_rows(feature_path, "perceptual feature analyzer")
    perceptual = index_validated_rows(
        feature_rows, labels, PERCEPTUAL_FIELDS, "perceptual feature analyzer"
    )
    validate_perceptual_rows(perceptual)
    return {label: (liveliness[label], perceptual[label]) for label in labels}


def load_cache(path: Path, analyzer_fingerprint: str) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if payload.get("schema") != SCHEMA:
        return {}
    if payload.get("analyzer", {}).get("fingerprint") != analyzer_fingerprint:
        return {}
    cache: dict[str, dict[str, Any]] = {}
    for record in payload.get("records", []):
        if not isinstance(record, dict):
            continue
        preview_sha = record.get("preview_sha256")
        cached_analysis_key = record.get("analysis_key_sha256")
        liveliness = record.get("liveliness")
        perceptual = record.get("perceptual")
        if (
            not isinstance(preview_sha, str)
            or not isinstance(cached_analysis_key, str)
            or not isinstance(liveliness, dict)
            or not isinstance(perceptual, dict)
            or any(finite_number(liveliness.get(name)) is False for name in LIVELINESS_FIELDS)
            or any(finite_number(perceptual.get(name)) is False for name in PERCEPTUAL_SCALAR_FIELDS)
            or any(finite_number(perceptual.get(name)) is False for name in MORPHOLOGY_SCALAR_FIELDS)
            or any(name not in perceptual for name in PERCEPTUAL_FIELDS)
        ):
            continue
        try:
            validate_perceptual_rows({"cached": perceptual})
        except AnalysisError:
            continue
        cache[cached_analysis_key] = record
    return cache


def parse_args() -> argparse.Namespace:
    default_runner = Path.home() / ".codex/skills/visual-liveliness/scripts/run.sh"
    default_feature_python = Path(
        os.environ.get(
            "VISUAL_LIVELINESS_PYTHON",
            str(Path.home() / ".cache/visual-liveliness/venv/bin/python"),
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--archive-snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--runner",
        type=Path,
        default=Path(os.environ.get("VISUAL_LIVELINESS_RUNNER", str(default_runner))),
    )
    parser.add_argument("--feature-python", type=Path, default=default_feature_python)
    parser.add_argument(
        "--feature-script", type=Path, default=Path(__file__).with_name("perceptual_image_features.py")
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, handle_process_signal)
    signal.signal(signal.SIGTERM, handle_process_signal)
    if args.batch_size < 1 or args.batch_size > 256:
        print("--batch-size must be between 1 and 256", file=sys.stderr)
        return 2

    run_dir = args.run_dir.expanduser().resolve()
    archive_path = (args.archive or run_dir / "archive.json").expanduser().resolve()
    archive_snapshot_path = (
        args.archive_snapshot or run_dir / "ranking-archive.json"
    ).expanduser().resolve()
    output_path = (args.output or run_dir / "image-analysis.json").expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    runner = args.runner.expanduser().resolve()
    feature_python = args.feature_python.expanduser().resolve()
    feature_script = args.feature_script.expanduser().resolve()
    for description, path in (
        ("visual-liveliness runner", runner),
        ("perceptual feature script", feature_script),
    ):
        if not path.is_file():
            print(f"{description} not found: {path}", file=sys.stderr)
            return 2

    try:
        run_selftests(runner, feature_python, feature_script)
        targets, archive_sha, archive_bytes, dry_reference = load_targets(run_dir, archive_path)
        analyzer_fingerprint = sha256_files([runner, runner.with_name("liveliness.py"), feature_script])
        cache = {} if args.force else load_cache(output_path, analyzer_fingerprint)
        analyzed: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, str]] = []
        reused = 0
        with tempfile.TemporaryDirectory(prefix=".glic-image-analysis-", dir=output_path.parent) as name:
            temporary_dir = Path(name)
            preview_cache = run_dir / "ranking-previews"
            preview_cache.mkdir(parents=True, exist_ok=True)
            stable_targets = [
                cache_preview(target, run_dir, preview_cache, temporary_dir) for target in targets
            ]
            for target in stable_targets:
                cached = cache.get(target["analysis_key_sha256"])
                if cached:
                    analyzed[target["label"]] = {
                        "candidate_id": target["candidate_id"],
                        "recipe_hash": target["recipe_hash"],
                        "preview_path": target["preview_path"],
                        "preview_sha256": target["preview_sha256"],
                        "analysis_key_sha256": target["analysis_key_sha256"],
                        "reference_path": target.get("reference_path", ""),
                        "reference_sha256": target.get("reference_sha256", ""),
                        "residual_mode": "dry_wet"
                        if target.get("reference_sha256")
                        else "wet_only_fallback",
                        "liveliness": {key: cached["liveliness"][key] for key in LIVELINESS_FIELDS},
                        "perceptual": {key: cached["perceptual"][key] for key in PERCEPTUAL_FIELDS},
                    }
                    reused += 1
                else:
                    pending.append(target)
            for start in range(0, len(pending), args.batch_size):
                batch = pending[start : start + args.batch_size]
                rows = analyze_batch(
                    batch, runner, feature_python, feature_script, temporary_dir, start // args.batch_size
                )
                for target in batch:
                    liveliness, perceptual = rows[target["label"]]
                    analyzed[target["label"]] = {
                        "candidate_id": target["candidate_id"],
                        "recipe_hash": target["recipe_hash"],
                        "preview_path": target["preview_path"],
                        "preview_sha256": target["preview_sha256"],
                        "analysis_key_sha256": target["analysis_key_sha256"],
                        "reference_path": target.get("reference_path", ""),
                        "reference_sha256": target.get("reference_sha256", ""),
                        "residual_mode": "dry_wet"
                        if target.get("reference_sha256")
                        else "wet_only_fallback",
                        "liveliness": {key: liveliness[key] for key in LIVELINESS_FIELDS},
                        "perceptual": {key: perceptual[key] for key in PERCEPTUAL_FIELDS},
                    }

        missing = [target["label"] for target in targets if target["label"] not in analyzed]
        if missing:
            raise AnalysisError(f"analysis is incomplete: {missing[:3]}")
        records = [analyzed[target["label"]] for target in targets]
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "schema": SCHEMA,
            "generated_at": generated_at,
            "source": {
                "archive_path": str(archive_path),
                "archive_snapshot_path": str(archive_snapshot_path),
                "archive_sha256": archive_sha,
                "run_dir": str(run_dir),
                "dry_reference_path": "" if dry_reference is None else str(dry_reference),
                "dry_reference_sha256": ""
                if dry_reference is None
                else (
                    targets[0]["reference_sha256"]
                    if targets
                    else sha256_file(dry_reference)
                ),
                "residual_mode": "dry_wet"
                if dry_reference is not None
                else "wet_only_fallback",
            },
            "analyzer": {
                "fingerprint": analyzer_fingerprint,
                "runner": str(runner),
                "runner_sha256": sha256_file(runner),
                "feature_script": str(feature_script),
                "feature_script_sha256": sha256_file(feature_script),
                "selftests": "passed",
            },
            "stats": {
                "expected": len(targets),
                "analyzed": len(records),
                "reused": reused,
                "new": len(pending),
                "dry_reference_available": dry_reference is not None,
            },
            "records": records,
        }
        atomic_write_text(archive_snapshot_path, archive_bytes.decode("utf-8"))
        atomic_write_text(
            output_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        )
    except (AnalysisError, OSError) as error:
        print(f"image analysis failed closed; previous result was preserved: {error}", file=sys.stderr)
        return 1

    print(
        f"wrote {len(records)} image analyses to {output_path} "
        f"({reused} cached, {len(pending)} new)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
