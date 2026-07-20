#!/usr/bin/env python3
"""Rank the original named GLIC presets on the Metal realtime visual path.

This adapter deliberately keeps three claims separate:

* the files are original named GLIC preset parameter files;
* timing is measured on GLIC Metal's realtime *visual approximation* path;
* pixel fidelity and 30 fps performance of the original Processing codec are
  not established by this report.

The benchmark is treated as untrusted input.  Every preset independently has
to prove Metal, 960x540, at least 10 warm-up frames, at least 120 measured
frames, and both mean and p95 wall time at or below the 30 fps frame budget.
Missing or malformed evidence rejects that preset rather than being imputed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterable, TextIO

import cv2
import numpy as np

from perceptual_image_features import extract_features


SCHEMA = "glic-original-preset-catalog-v1"
BENCHMARK_SCHEMA = "glic-realtime-benchmark-v1"
MANIFEST_SCHEMA = "glic-original-preset-preview-manifest-v1"
EXPECTED_BACKEND = "metal"
EXPECTED_WIDTH = 960
EXPECTED_HEIGHT = 540
MIN_WARMUP_FRAMES = 10
MIN_MEASURED_FRAMES = 120
REQUIRED_FPS = 30.0
MAX_FRAME_MS = 1000.0 / REQUIRED_FPS
DEFAULT_EXPECTED_PRESETS = 144
DEFAULT_SELECTION_COUNT = 12
UPSTREAM_PRESET_MANIFEST_SCHEMA = "glic-upstream-preset-manifest-v1"
UPSTREAM_PRESET_COMMIT = "460e61bf9b01f7415cf973b3d655a0ae2c7962a7"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PRESET_MANIFEST = REPOSITORY_ROOT / "presets.upstream.sha256"
_SOURCE_PRESET_DIRECTORY = REPOSITORY_ROOT / "presets"
_INSTALLED_PRESET_ROOT = REPOSITORY_ROOT / "share" / "glic-metal"
DEFAULT_UPSTREAM_PRESET_MANIFEST = (
    _SOURCE_PRESET_MANIFEST
    if _SOURCE_PRESET_MANIFEST.is_file()
    else _INSTALLED_PRESET_ROOT / "presets.upstream.sha256"
)
DEFAULT_UPSTREAM_PRESET_DIRECTORY = (
    _SOURCE_PRESET_DIRECTORY
    if _SOURCE_PRESET_DIRECTORY.is_dir()
    else _INSTALLED_PRESET_ROOT / "presets"
)
MIN_VISIBLE_MAE_RGB = 10.0
MIN_VISIBLE_CHANGED_PIXEL_RATIO = 0.25
MAX_VISIBLE_LUMA_SSIM = 0.95
MAX_HIGHLIGHT_CLIPPING_RATIO = 0.15
MIN_SELECTED_MORPHOLOGY_DISTANCE = 0.10
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

FIDELITY = {
    "preset_files": "original_glic_named_preset_corpus",
    "preset_parameters": "per_row_mapping_fidelity_required",
    "render_path": "glic_metal_realtime_visual_approximation",
    "codec_pixel_fidelity": "not_claimed",
    "original_processing_codec_performance": "not_measured",
    "label": (
        "Original preset parameters rendered by the GLIC Metal realtime visual "
        "approximation; this is not a pixel-fidelity or performance claim for "
        "the original Processing codec."
    ),
}
MAPPING_FIDELITIES = ("exact-compatible", "approximated", "unsupported")


class CatalogError(RuntimeError):
    """Raised when a source is too malformed to audit safely."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> Any:
    raise CatalogError(f"non-finite JSON number: {value}")


def strict_json_load(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogError(f"cannot read {path}: {error}") from error
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (CatalogError, json.JSONDecodeError) as error:
        raise CatalogError(f"{path} is not strict JSON: {error}") from error


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def frame_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise CatalogError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def verify_upstream_preset_corpus(
    manifest_path: Path | None, corpus_dir: Path | None
) -> dict[str, Any]:
    """Verify the pinned 144-file upstream corpus and return audit evidence."""
    if manifest_path is None or corpus_dir is None:
        return {
            "verified": False,
            "reasons": ["upstream_preset_corpus_evidence_not_supplied"],
            "manifest_names": [],
            "upstream_commit": None,
            "manifest": None,
            "directory": None,
        }
    if not manifest_path.is_file():
        raise CatalogError(f"upstream preset manifest does not exist: {manifest_path}")
    if not corpus_dir.is_dir():
        raise CatalogError(f"upstream preset directory does not exist: {corpus_dir}")

    metadata: dict[str, str] = {}
    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line:
            continue
        if raw_line.startswith("# "):
            key, separator, value = raw_line[2:].partition("=")
            if not separator or not key or key in metadata:
                raise CatalogError(f"invalid manifest metadata at line {line_number}")
            metadata[key] = value
            continue
        digest, separator, name = raw_line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not name
            or Path(name).name != name
            or name in expected
        ):
            raise CatalogError(f"invalid manifest entry at line {line_number}")
        expected[name] = digest

    try:
        declared_count = int(metadata.get("count", ""))
    except ValueError as error:
        raise CatalogError("upstream preset manifest count is invalid") from error
    if metadata.get("schema") != UPSTREAM_PRESET_MANIFEST_SCHEMA:
        raise CatalogError("upstream preset manifest schema is invalid")
    if metadata.get("commit") != UPSTREAM_PRESET_COMMIT:
        raise CatalogError("upstream preset manifest commit is not pinned")
    if declared_count != DEFAULT_EXPECTED_PRESETS or len(expected) != declared_count:
        raise CatalogError("upstream preset manifest must contain exactly 144 files")

    actual_names = {
        item.name for item in corpus_dir.iterdir() if item.is_file()
    }
    expected_names = set(expected)
    reasons: list[str] = []
    if actual_names != expected_names:
        reasons.append("local_preset_name_set_differs_from_pinned_upstream")
    mismatches: list[str] = []
    for name, digest in expected.items():
        candidate = corpus_dir / name
        if not candidate.is_file() or sha256_file(candidate) != digest:
            mismatches.append(name)
    if mismatches:
        reasons.append("local_preset_sha256_mismatch")

    return {
        "verified": not reasons,
        "reasons": reasons,
        "manifest_names": sorted(expected_names, key=lambda value: (value.casefold(), value)),
        "mismatched_files": sorted(mismatches, key=str.casefold),
        "upstream_commit": UPSTREAM_PRESET_COMMIT,
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "directory": str(corpus_dir.resolve()),
    }


def _row_or_root_count(row: dict[str, Any], root: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in row:
            return frame_count(row[key])
    for key in keys:
        if key in root:
            return frame_count(root[key])
    return None


def performance_gate_reasons(row: dict[str, Any] | None, root: dict[str, Any]) -> list[str]:
    """Return fail-closed reasons for the mandatory realtime timing gate."""
    if row is None:
        return ["missing_benchmark_record"]

    reasons: list[str] = []
    if root.get("width") != EXPECTED_WIDTH or root.get("height") != EXPECTED_HEIGHT:
        reasons.append("benchmark_resolution_mismatch")
    if row.get("width", EXPECTED_WIDTH) != EXPECTED_WIDTH or row.get(
        "height", EXPECTED_HEIGHT
    ) != EXPECTED_HEIGHT:
        reasons.append("row_resolution_mismatch")
    if row.get("backend") != EXPECTED_BACKEND:
        reasons.append("backend_is_not_metal")

    warmup = _row_or_root_count(row, root, "warmup_frames")
    measured = _row_or_root_count(
        row, root, "frames_measured", "completed_frames", "frames"
    )
    if warmup is None or warmup < MIN_WARMUP_FRAMES:
        reasons.append("insufficient_warmup_frames")
    if measured is None or measured < MIN_MEASURED_FRAMES:
        reasons.append("insufficient_measured_frames")

    if row.get("process_passed") is not True:
        reasons.append("processing_did_not_pass")
    error = row.get("error", "")
    if not isinstance(error, str) or error:
        reasons.append("benchmark_reported_error")

    mean_ms = finite_number(row.get("mean_ms"))
    p95_ms = finite_number(row.get("p95_ms"))
    if mean_ms is None or mean_ms <= 0.0:
        reasons.append("missing_or_invalid_mean_ms")
    elif mean_ms > MAX_FRAME_MS:
        reasons.append("mean_slower_than_30fps")
    if p95_ms is None or p95_ms <= 0.0:
        reasons.append("missing_or_invalid_p95_ms")
    elif p95_ms > MAX_FRAME_MS:
        reasons.append("p95_slower_than_30fps")
    return reasons


def mapping_gate(row: dict[str, Any] | None, root: dict[str, Any]) -> dict[str, Any]:
    """Classify upstream parameter mapping without upgrading approximations."""
    reasons: list[str] = []
    semantics = root.get("preset_semantics")
    if semantics != "original":
        reasons.append("preset_semantics_is_not_original")
    if root.get("processing_mode") != "compat_realtime":
        reasons.append("processing_mode_is_not_compat_realtime")
    if row is None:
        return {
            "tier": "unsupported",
            "fidelity": None,
            "reasons": reasons + ["missing_benchmark_record"],
            "reported_reasons": [],
            "original_compatible": False,
            "approximation_tier": False,
        }

    fidelity = row.get("preset_mapping_fidelity")
    reported_reasons = row.get("preset_mapping_reasons")
    if fidelity not in MAPPING_FIDELITIES:
        reasons.append("missing_or_invalid_preset_mapping_fidelity")
        fidelity = None
    if not isinstance(reported_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reported_reasons
    ):
        reasons.append("invalid_preset_mapping_reasons")
        reported_reasons = []
    elif fidelity == "exact-compatible" and reported_reasons:
        reasons.append("exact_mapping_must_not_report_approximation_reasons")
    elif fidelity in ("approximated", "unsupported") and not reported_reasons:
        reasons.append("missing_preset_mapping_reasons")

    if fidelity == "exact-compatible" and not reasons:
        tier = "exact-compatible"
    elif fidelity == "approximated" and not reasons:
        tier = "approximated"
    else:
        # Unsupported and unreported mappings are never promoted to the
        # approximated tier, even if the realtime shader happens to run them.
        tier = "unsupported"
    if fidelity == "unsupported":
        reasons.append("preset_mapping_unsupported")
    elif fidelity == "approximated":
        reasons.append("preset_mapping_approximated")

    return {
        "tier": tier,
        "fidelity": fidelity,
        "reasons": reasons,
        "reported_reasons": reported_reasons,
        "original_compatible": tier == "exact-compatible",
        "approximation_tier": tier == "approximated",
    }


def load_benchmark(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    document = strict_json_load(path)
    if not isinstance(document, dict):
        raise CatalogError("benchmark root must be an object")
    if document.get("schema") != BENCHMARK_SCHEMA:
        raise CatalogError(f"unsupported benchmark schema: {document.get('schema')!r}")
    rows = document.get("results")
    if not isinstance(rows, list):
        raise CatalogError("benchmark results must be an array")

    by_preset: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CatalogError(f"benchmark result {index} is not an object")
        preset = row.get("preset")
        if not isinstance(preset, str) or not preset or preset in (".", ".."):
            raise CatalogError(f"benchmark result {index} has an invalid preset name")
        if preset in by_preset:
            raise CatalogError(f"benchmark has duplicate preset record: {preset}")
        by_preset[preset] = row
    return document, by_preset


def load_liveliness(path: Path) -> tuple[dict[str, dict[str, float]], dict[str, list[str]]]:
    """Load external visual-liveliness rows without using them as aesthetics.

    Invalid or incomplete rows are retained as per-preset QA reasons.  The
    whole file must still be strict JSON with unique labels, which prevents a
    silently partial or ambiguous join.
    """
    document = strict_json_load(path)
    if not isinstance(document, list):
        raise CatalogError("liveliness JSON must be an array of labelled rows")
    valid: dict[str, dict[str, float]] = {}
    invalid: dict[str, list[str]] = {}
    seen: set[str] = set()
    for index, row in enumerate(document):
        if not isinstance(row, dict):
            raise CatalogError(f"liveliness row {index} is not an object")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError(f"liveliness row {index} has no string name")
        if name in seen:
            raise CatalogError(f"liveliness has duplicate preset label: {name}")
        seen.add(name)
        missing = [field for field in LIVELINESS_FIELDS if finite_number(row.get(field)) is None]
        if missing:
            invalid[name] = ["missing_or_nonfinite:" + ",".join(missing)]
        else:
            valid[name] = {field: float(row[field]) for field in LIVELINESS_FIELDS}
    return valid, invalid


def _manifest_rows(document: Any) -> Iterable[tuple[str, str]]:
    if isinstance(document, dict) and "previews" not in document:
        # Compact form: {"preset name": "relative/or/absolute.png"}
        for preset, path in document.items():
            if not isinstance(preset, str) or not isinstance(path, str):
                raise CatalogError("compact preview manifest must map strings to strings")
            yield preset, path
        return

    if isinstance(document, dict):
        schema = document.get("schema")
        if schema not in (None, MANIFEST_SCHEMA):
            raise CatalogError(f"unsupported preview manifest schema: {schema!r}")
        document = document.get("previews")
    if not isinstance(document, list):
        raise CatalogError("preview manifest must be a mapping or an array")
    for index, row in enumerate(document):
        if not isinstance(row, dict):
            raise CatalogError(f"preview manifest row {index} is not an object")
        preset, path = row.get("preset"), row.get("path")
        if not isinstance(preset, str) or not preset or not isinstance(path, str) or not path:
            raise CatalogError(f"preview manifest row {index} needs string preset and path")
        yield preset, path


def load_preview_manifest(path: Path) -> dict[str, Path]:
    document = strict_json_load(path)
    result: dict[str, Path] = {}
    for preset, raw_path in _manifest_rows(document):
        if preset in result:
            raise CatalogError(f"preview manifest has duplicate preset: {preset}")
        preview = Path(raw_path).expanduser()
        if not preview.is_absolute():
            preview = path.parent / preview
        result[preset] = preview.resolve()
    return result


def discover_previews(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        raise CatalogError(f"preview directory does not exist: {directory}")
    result: dict[str, Path] = {}
    for preview in sorted(directory.rglob("*.png")):
        preset = preview.stem
        if preset in result:
            raise CatalogError(
                f"preview filename stems are ambiguous for {preset}: "
                f"{result[preset]} and {preview}"
            )
        result[preset] = preview.resolve()
    return result


def decode_image(path: Path, description: str) -> np.ndarray:
    if not path.is_file():
        raise CatalogError(f"{description} does not exist: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise CatalogError(f"cannot decode {description}: {path}")
    if image.ndim != 3 or image.shape[2] != 3:
        raise CatalogError(f"{description} is not a three-channel image: {path}")
    return image


def _round(value: float) -> float:
    if not math.isfinite(float(value)):
        raise CatalogError("analysis produced a non-finite value")
    return round(float(value), 8)


def difference_metrics(dry: np.ndarray, wet: np.ndarray) -> dict[str, float]:
    if dry.shape != wet.shape:
        raise CatalogError(
            f"dry/wet dimensions differ: {dry.shape[1]}x{dry.shape[0]} vs "
            f"{wet.shape[1]}x{wet.shape[0]}"
        )
    dry_float = dry.astype(np.float64)
    wet_float = wet.astype(np.float64)
    absolute = np.abs(wet_float - dry_float)
    maximum = absolute.max(axis=2)
    squared = np.square(wet_float - dry_float)

    dry_luma = cv2.cvtColor(dry, cv2.COLOR_BGR2GRAY).astype(np.float64)
    wet_luma = cv2.cvtColor(wet, cv2.COLOR_BGR2GRAY).astype(np.float64)
    luma_absolute = np.abs(wet_luma - dry_luma)

    dry_centered = dry_luma - dry_luma.mean()
    wet_centered = wet_luma - wet_luma.mean()
    denominator = math.sqrt(
        float(np.square(dry_centered).sum()) * float(np.square(wet_centered).sum())
    )
    if denominator > 1e-12:
        correlation = float((dry_centered * wet_centered).sum()) / denominator
    else:
        correlation = 1.0 if np.array_equal(dry, wet) else 0.0

    # Global SSIM proxy.  It is deterministic and dependency-free, and is
    # labelled as global rather than pretending to be windowed SSIM.
    dry_mean = float(dry_luma.mean())
    wet_mean = float(wet_luma.mean())
    dry_variance = float(np.square(dry_luma - dry_mean).mean())
    wet_variance = float(np.square(wet_luma - wet_mean).mean())
    covariance = float(((dry_luma - dry_mean) * (wet_luma - wet_mean)).mean())
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    global_ssim = (
        (2.0 * dry_mean * wet_mean + c1) * (2.0 * covariance + c2)
    ) / (
        (dry_mean * dry_mean + wet_mean * wet_mean + c1)
        * (dry_variance + wet_variance + c2)
    )

    # Match the repository's video VISIBLE gate with local Gaussian-window
    # luminance SSIM. The global statistic is retained as a descriptor, but it
    # can overstate similarity when local block damage is strong.
    dry_luma32 = dry_luma.astype(np.float32)
    wet_luma32 = wet_luma.astype(np.float32)
    mean_dry = cv2.GaussianBlur(dry_luma32, (11, 11), 1.5)
    mean_wet = cv2.GaussianBlur(wet_luma32, (11, 11), 1.5)
    mean_dry_squared = mean_dry * mean_dry
    mean_wet_squared = mean_wet * mean_wet
    mean_product = mean_dry * mean_wet
    variance_dry = cv2.GaussianBlur(dry_luma32 * dry_luma32, (11, 11), 1.5)
    variance_dry -= mean_dry_squared
    variance_wet = cv2.GaussianBlur(wet_luma32 * wet_luma32, (11, 11), 1.5)
    variance_wet -= mean_wet_squared
    covariance_local = cv2.GaussianBlur(dry_luma32 * wet_luma32, (11, 11), 1.5)
    covariance_local -= mean_product
    local_numerator = (2.0 * mean_product + c1) * (2.0 * covariance_local + c2)
    local_denominator = (mean_dry_squared + mean_wet_squared + c1) * (
        variance_dry + variance_wet + c2
    )
    windowed_ssim = float(
        np.mean(local_numerator / np.maximum(local_denominator, 1.0e-9))
    )

    shadow_clipping = wet <= 1
    highlight_clipping = wet >= 254
    clipping = np.logical_or(shadow_clipping, highlight_clipping)
    return {
        "mae_rgb": _round(absolute.mean()),
        "rmse_rgb": _round(math.sqrt(float(squared.mean()))),
        "luma_mae": _round(luma_absolute.mean()),
        "changed_pixel_ratio": _round(float(np.count_nonzero(maximum >= 10.0)) / maximum.size),
        "strong_changed_pixel_ratio": _round(
            float(np.count_nonzero(maximum >= 32.0)) / maximum.size
        ),
        "luma_correlation": _round(max(-1.0, min(1.0, correlation))),
        "global_luma_ssim": _round(max(-1.0, min(1.0, global_ssim))),
        "windowed_luma_ssim": _round(max(-1.0, min(1.0, windowed_ssim))),
        "output_clipping_ratio": _round(float(np.count_nonzero(clipping)) / clipping.size),
        "shadow_clipping_ratio": _round(
            float(np.count_nonzero(shadow_clipping)) / shadow_clipping.size
        ),
        "highlight_clipping_ratio": _round(
            float(np.count_nonzero(highlight_clipping)) / highlight_clipping.size
        ),
    }


def visual_gate_reasons(metrics: dict[str, float] | None) -> list[str]:
    """Reject invisible or technically collapsed stills before ranking.

    These thresholds mirror the repository's VISIBLE dry/wet video gate. They
    are technical integrity checks, not an aesthetic score.
    """
    if metrics is None:
        return ["missing_dry_wet_metrics"]
    reasons: list[str] = []
    if metrics["mae_rgb"] < MIN_VISIBLE_MAE_RGB:
        reasons.append("effect_mae_below_visible_floor")
    if metrics["changed_pixel_ratio"] < MIN_VISIBLE_CHANGED_PIXEL_RATIO:
        reasons.append("changed_pixel_ratio_below_visible_floor")
    if metrics["windowed_luma_ssim"] > MAX_VISIBLE_LUMA_SSIM:
        reasons.append("source_similarity_above_visible_ceiling")
    if metrics["highlight_clipping_ratio"] > MAX_HIGHLIGHT_CLIPPING_RATIO:
        reasons.append("severe_highlight_clipping")
    return reasons


def visibility_score(metrics: dict[str, float], perceptual: dict[str, Any]) -> float:
    """A technical effect-presence score, not an aesthetic quality score."""
    mae = min(1.0, metrics["mae_rgb"] / 64.0)
    luma = min(1.0, metrics["luma_mae"] / 48.0)
    changed = metrics["changed_pixel_ratio"]
    strong = metrics["strong_changed_pixel_ratio"]
    residual = min(1.0, float(perceptual["residual_mask_coverage"]) / 0.75)
    return _round(0.25 * mae + 0.15 * luma + 0.25 * changed + 0.2 * strong + 0.15 * residual)


def _rms_distance(left: Any, right: Any) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    if a.shape != b.shape or a.size == 0 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise CatalogError("perceptual descriptor shapes are invalid")
    return min(1.0, math.sqrt(float(np.square(a - b).mean())))


def _histogram_distance(left: Any, right: Any) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    if a.shape != b.shape or a.size == 0 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise CatalogError("perceptual histogram shapes are invalid")
    return min(1.0, 0.5 * float(np.abs(a - b).sum()))


def _hash_distance(left: Any, right: Any) -> float:
    if not isinstance(left, str) or not isinstance(right, str) or len(left) != 16 or len(right) != 16:
        raise CatalogError("perceptual hashes must be fixed-width 64-bit hex strings")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count() / 64.0
    except ValueError as error:
        raise CatalogError("perceptual hash contains non-hex characters") from error


def visual_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Weighted dry/wet morphology distance in the closed interval [0, 1]."""
    components = (
        (0.26, _rms_distance(left["residual_luma_grid"], right["residual_luma_grid"])),
        (0.18, _rms_distance(left["residual_edge_grid"], right["residual_edge_grid"])),
        (
            0.14,
            _histogram_distance(
                left["residual_scale_histogram"], right["residual_scale_histogram"]
            ),
        ),
        (0.10, _rms_distance(left["residual_orientation"], right["residual_orientation"])),
        (0.12, _hash_distance(left["residual_phash"], right["residual_phash"])),
        (0.08, _histogram_distance(left["hsv_hist"], right["hsv_hist"])),
        (0.07, _rms_distance(left["color_grid"], right["color_grid"])),
        (0.05, _hash_distance(left["phash"], right["phash"])),
    )
    return _round(sum(weight * value for weight, value in components))


def greedy_diversity_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a deterministic farthest-first order over eligible rows."""
    if not rows:
        return []
    remaining = {row["preset"]: row for row in rows}
    first = min(
        rows,
        key=lambda row: (-float(row["effect_presence_score"]), row["preset"].casefold(), row["preset"]),
    )
    order = [first]
    del remaining[first["preset"]]
    first["min_distance_to_earlier"] = None
    first["diversity_selection_score"] = _round(float(first["effect_presence_score"]))

    while remaining:
        scored: list[tuple[float, float, str, dict[str, Any]]] = []
        for preset, candidate in remaining.items():
            minimum = min(
                visual_distance(candidate["perceptual"], selected["perceptual"])
                for selected in order
            )
            # Dissimilarity dominates.  A small presence term prevents a nearly
            # dry image from winning a numerical tie between morphologies.
            combined = 0.90 * minimum + 0.10 * float(candidate["effect_presence_score"])
            scored.append((combined, minimum, preset, candidate))
        combined, minimum, preset, chosen = min(
            scored,
            key=lambda item: (-item[0], -item[1], item[2].casefold(), item[2]),
        )
        chosen["min_distance_to_earlier"] = _round(minimum)
        chosen["diversity_selection_score"] = _round(combined)
        order.append(chosen)
        del remaining[preset]
    return order


def apply_diversity_selection(
    ordered: list[dict[str, Any]],
    select_count: int,
    *,
    rank_field: str,
    selected_field: str,
) -> None:
    """Mark at most ``select_count`` rows without filling from duplicates."""
    for index, row in enumerate(ordered, 1):
        row[rank_field] = index
        row[selected_field] = bool(
            index <= select_count
            and (
                index == 1
                or float(row["min_distance_to_earlier"])
                >= MIN_SELECTED_MORPHOLOGY_DISTANCE
            )
        )


def _gate_summary(row: dict[str, Any] | None, root: dict[str, Any]) -> dict[str, Any]:
    reasons = performance_gate_reasons(row, root)
    mean_ms = None if row is None else finite_number(row.get("mean_ms"))
    p95_ms = None if row is None else finite_number(row.get("p95_ms"))
    warmup = None if row is None else _row_or_root_count(row, root, "warmup_frames")
    measured = (
        None
        if row is None
        else _row_or_root_count(row, root, "frames_measured", "completed_frames", "frames")
    )
    return {
        "passed": not reasons,
        "reasons": reasons,
        "backend": None if row is None else row.get("backend"),
        "width": root.get("width"),
        "height": root.get("height"),
        "warmup_frames": warmup,
        "frames_measured": measured,
        "mean_ms": mean_ms,
        "p95_ms": p95_ms,
        "mean_fps": None if mean_ms is None or mean_ms <= 0.0 else _round(1000.0 / mean_ms),
        "p95_fps": None if p95_ms is None or p95_ms <= 0.0 else _round(1000.0 / p95_ms),
    }


def build_catalog(
    benchmark_path: Path,
    previews: dict[str, Path],
    dry_path: Path,
    *,
    expected_count: int = DEFAULT_EXPECTED_PRESETS,
    select_count: int = DEFAULT_SELECTION_COUNT,
    liveliness_path: Path | None = None,
    corpus_manifest_path: Path | None = None,
    preset_corpus_dir: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if expected_count <= 0:
        raise CatalogError("expected preset count must be positive")
    if select_count <= 0:
        raise CatalogError("selection count must be positive")

    root, benchmark_rows = load_benchmark(benchmark_path)
    liveliness_rows: dict[str, dict[str, float]] = {}
    invalid_liveliness: dict[str, list[str]] = {}
    if liveliness_path is not None:
        liveliness_rows, invalid_liveliness = load_liveliness(liveliness_path)
    dry = decode_image(dry_path, "dry reference")
    dry_height, dry_width = dry.shape[:2]
    if (dry_width, dry_height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise CatalogError(
            f"dry reference must be {EXPECTED_WIDTH}x{EXPECTED_HEIGHT}, got "
            f"{dry_width}x{dry_height}"
        )

    benchmark_names = set(benchmark_rows)
    preview_names = set(previews)
    provenance = verify_upstream_preset_corpus(
        corpus_manifest_path, preset_corpus_dir
    )
    provenance_reasons = list(provenance["reasons"])
    manifest_names = set(provenance["manifest_names"])
    if provenance["verified"]:
        if benchmark_names != manifest_names:
            provenance_reasons.append(
                "benchmark_name_set_differs_from_pinned_upstream"
            )
        if preview_names != manifest_names:
            provenance_reasons.append(
                "preview_name_set_differs_from_pinned_upstream"
            )
    provenance["reasons"] = provenance_reasons
    provenance["verified"] = bool(provenance["verified"] and not provenance_reasons)
    fidelity = dict(FIDELITY)
    fidelity["preset_files"] = (
        "sha256_verified_pinned_upstream_corpus"
        if provenance["verified"]
        else "unverified_preset_corpus"
    )
    fidelity["upstream_commit"] = provenance["upstream_commit"]
    all_names = sorted(benchmark_names | preview_names, key=lambda value: (value.casefold(), value))
    results: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []

    for preset in all_names:
        benchmark_row = benchmark_rows.get(preset)
        gate = _gate_summary(benchmark_row, root)
        mapping = mapping_gate(benchmark_row, root)
        preview_path = previews.get(preset)
        preview_reasons: list[str] = []
        perceptual: dict[str, Any] | None = None
        difference: dict[str, float] | None = None
        preview_width = preview_height = None
        preview_sha256 = None
        if preview_path is None:
            preview_reasons.append("missing_preview")
        elif not preview_path.is_file():
            preview_reasons.append("preview_file_missing")
        else:
            try:
                wet = decode_image(preview_path, f"preview for {preset}")
                preview_height, preview_width = wet.shape[:2]
                if (preview_width, preview_height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
                    preview_reasons.append("preview_resolution_mismatch")
                else:
                    difference = difference_metrics(dry, wet)
                    perceptual = extract_features(wet, dry)
                    preview_sha256 = sha256_file(preview_path)
            except (CatalogError, ValueError, KeyError) as error:
                preview_reasons.append(f"preview_analysis_failed:{type(error).__name__}")

        visual_reasons = visual_gate_reasons(difference)

        row = {
            "preset": preset,
            "eligible": bool(
                mapping["original_compatible"]
                and gate["passed"]
                and not preview_reasons
                and not visual_reasons
                and perceptual is not None
            ),
            "approximation_eligible": bool(
                mapping["approximation_tier"]
                and gate["passed"]
                and not preview_reasons
                and not visual_reasons
                and perceptual is not None
            ),
            "selected": False,
            "approximation_selected": False,
            "rank": None,
            "approximation_rank": None,
            "fidelity": dict(fidelity),
            "preset_mapping": mapping,
            "performance_gate": gate,
            "preview_gate": {"passed": not preview_reasons, "reasons": preview_reasons},
            "visual_integrity_gate": {
                "passed": not visual_reasons,
                "reasons": visual_reasons,
            },
            "preview": {
                "path": None if preview_path is None else str(preview_path),
                "sha256": preview_sha256,
                "width": preview_width,
                "height": preview_height,
            },
            "difference": difference,
            "perceptual": perceptual,
            # External liveliness is a separate technical presence/shape metric
            # family.  It has no weight in effect presence or diversity order.
            "liveliness_gate": {
                "passed": liveliness_path is None
                or (preset in liveliness_rows and preset not in invalid_liveliness),
                "reasons": (
                    []
                    if liveliness_path is None or preset in liveliness_rows
                    else invalid_liveliness.get(preset, ["missing_liveliness_row"])
                ),
            },
            "liveliness": liveliness_rows.get(preset),
            "effect_presence_score": (
                None if difference is None or perceptual is None else visibility_score(difference, perceptual)
            ),
            "min_distance_to_earlier": None,
            "diversity_selection_score": None,
        }
        results.append(row)
        if row["eligible"]:
            eligible.append(row)

    ordered = greedy_diversity_order(eligible)
    apply_diversity_selection(
        ordered,
        select_count,
        rank_field="rank",
        selected_field="selected",
    )

    approximation_eligible = [row for row in results if row["approximation_eligible"]]
    approximation_ordered = greedy_diversity_order(approximation_eligible)
    apply_diversity_selection(
        approximation_ordered,
        select_count,
        rank_field="approximation_rank",
        selected_field="approximation_selected",
    )

    benchmark_only = sorted(benchmark_names - preview_names, key=str.casefold)
    preview_only = sorted(preview_names - benchmark_names, key=str.casefold)
    corpus_complete = (
        len(benchmark_names) == expected_count
        and len(preview_names) == expected_count
        and not benchmark_only
        and not preview_only
    )
    selected = [row for row in ordered if row["selected"]]
    approximation_selected = [
        row for row in approximation_ordered if row["approximation_selected"]
    ]
    pairwise = [
        visual_distance(left["perceptual"], right["perceptual"])
        for left_index, left in enumerate(selected)
        for right in selected[left_index + 1 :]
    ]
    if not corpus_complete:
        status = "incomplete"
    elif not ordered and not approximation_ordered:
        status = "no_eligible_presets"
    else:
        status = "ok"

    publication_blockers: list[str] = []
    if status != "ok":
        publication_blockers.append(f"catalog_status:{status}")
    if not provenance["verified"]:
        publication_blockers.extend(provenance["reasons"])
    if not selected and not approximation_selected:
        publication_blockers.append("no_diverse_selected_presets")

    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "publishable": not publication_blockers,
        "publication_blockers": publication_blockers,
        "fidelity": fidelity,
        "upstream_provenance": provenance,
        "policy": {
            "backend": EXPECTED_BACKEND,
            "width": EXPECTED_WIDTH,
            "height": EXPECTED_HEIGHT,
            "minimum_warmup_frames": MIN_WARMUP_FRAMES,
            "minimum_measured_frames": MIN_MEASURED_FRAMES,
            "required_fps": REQUIRED_FPS,
            "maximum_mean_ms": MAX_FRAME_MS,
            "maximum_p95_ms": MAX_FRAME_MS,
            "minimum_visible_mae_rgb": MIN_VISIBLE_MAE_RGB,
            "minimum_visible_changed_pixel_ratio": MIN_VISIBLE_CHANGED_PIXEL_RATIO,
            "maximum_visible_windowed_luma_ssim": MAX_VISIBLE_LUMA_SSIM,
            "maximum_highlight_clipping_ratio": MAX_HIGHLIGHT_CLIPPING_RATIO,
            "minimum_selected_morphology_distance": MIN_SELECTED_MORPHOLOGY_DISTANCE,
            "performance_scope": "glic_metal_realtime_visual_approximation_only",
            "selection": "deterministic_greedy_farthest_first_dry_wet_morphology",
            "metric_families": {
                "mandatory_gate": "realtime_performance",
                "visual_integrity_gate": "visible_dry_wet_change_and_highlight_health",
                "parameter_compatibility": "preset_mapping_fidelity_separate_tiers",
                "effect_presence": "dry_wet_pixel_difference",
                "diversity": "dry_wet_perceptual_morphology",
                "technical_presence_shape": "optional_visual_liveliness_unscored",
                "aesthetic_quality": "not_scored",
            },
        },
        "sources": {
            "benchmark": str(benchmark_path.resolve()),
            "benchmark_sha256": sha256_file(benchmark_path),
            "dry_reference": str(dry_path.resolve()),
            "dry_reference_sha256": sha256_file(dry_path),
            "liveliness": None if liveliness_path is None else str(liveliness_path.resolve()),
            "liveliness_sha256": (
                None if liveliness_path is None else sha256_file(liveliness_path)
            ),
            "upstream_preset_manifest": provenance["manifest"],
            "upstream_preset_manifest_sha256": provenance.get("manifest_sha256"),
            "upstream_preset_directory": provenance["directory"],
        },
        "summary": {
            "expected_presets": expected_count,
            "benchmark_records": len(benchmark_names),
            "previews": len(preview_names),
            "union_presets": len(all_names),
            "corpus_complete": corpus_complete,
            "upstream_provenance_verified": provenance["verified"],
            "benchmark_without_preview": benchmark_only,
            "preview_without_benchmark": preview_only,
            "performance_passed": sum(bool(row["performance_gate"]["passed"]) for row in results),
            "performance_failed": sum(not bool(row["performance_gate"]["passed"]) for row in results),
            "analysis_passed": sum(bool(row["preview_gate"]["passed"]) for row in results),
            "visual_integrity_passed": sum(
                bool(row["visual_integrity_gate"]["passed"]) for row in results
            ),
            "visual_integrity_failed": sum(
                not bool(row["visual_integrity_gate"]["passed"]) for row in results
            ),
            "eligible": len(ordered),
            "selected": len(selected),
            "mapping_exact_compatible": sum(
                row["preset_mapping"]["tier"] == "exact-compatible" for row in results
            ),
            "mapping_approximated": sum(
                row["preset_mapping"]["tier"] == "approximated" for row in results
            ),
            "mapping_unsupported": sum(
                row["preset_mapping"]["tier"] == "unsupported" for row in results
            ),
            "original_compatible_eligible": len(ordered),
            "approximated_eligible": len(approximation_ordered),
            "approximated_selected": len(approximation_selected),
            "requested_selection": select_count,
            "minimum_selected_pair_distance": None if not pairwise else _round(min(pairwise)),
            "liveliness_requested": liveliness_path is not None,
            "liveliness_valid": sum(row["liveliness"] is not None for row in results),
            "liveliness_missing_or_invalid": sum(
                not row["liveliness_gate"]["passed"] for row in results
            ),
            "liveliness_without_corpus_preset": sorted(
                (set(liveliness_rows) | set(invalid_liveliness)) - set(all_names), key=str.casefold
            ),
        },
        "ranking": [row["preset"] for row in ordered],
        "rankings": {
            "original_compatible": [row["preset"] for row in ordered],
            "approximated": [row["preset"] for row in approximation_ordered],
            "unsupported": sorted(
                (
                    row["preset"]
                    for row in results
                    if row["preset_mapping"]["tier"] == "unsupported"
                ),
                key=str.casefold,
            ),
        },
        "results": results,
    }


def atomic_write(path: Path, writer: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, document: dict[str, Any]) -> None:
    def writer(handle: TextIO) -> None:
        json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")

    atomic_write(path, writer)


CSV_FIELDS = (
    "rank",
    "approximation_rank",
    "selected",
    "approximation_selected",
    "eligible",
    "approximation_eligible",
    "preset",
    "preset_mapping_tier",
    "preset_mapping_fidelity",
    "preset_mapping_reasons",
    "performance_passed",
    "performance_reasons",
    "mean_ms",
    "p95_ms",
    "mean_fps",
    "p95_fps",
    "preview_passed",
    "preview_reasons",
    "visual_integrity_passed",
    "visual_integrity_reasons",
    "mae_rgb",
    "changed_pixel_ratio",
    "strong_changed_pixel_ratio",
    "luma_correlation",
    "global_luma_ssim",
    "windowed_luma_ssim",
    "highlight_clipping_ratio",
    "effect_presence_score",
    "min_distance_to_earlier",
    "artifact_scale_bucket",
    "artifact_orientation",
    "liveliness_passed",
    "liveliness_reasons",
    "liveliness_lum_mean",
    "liveliness_occ_soft",
    "liveliness_blobs",
    "liveliness_shape_entropy",
    "preview_path",
    "render_path_fidelity",
    "codec_pixel_fidelity",
)


def _csv_row(row: dict[str, Any]) -> dict[str, Any]:
    performance = row["performance_gate"]
    preview = row["preview_gate"]
    visual = row["visual_integrity_gate"]
    difference = row.get("difference") or {}
    perceptual = row.get("perceptual") or {}
    liveliness = row.get("liveliness") or {}
    return {
        "rank": "" if row["rank"] is None else row["rank"],
        "approximation_rank": (
            "" if row["approximation_rank"] is None else row["approximation_rank"]
        ),
        "selected": row["selected"],
        "approximation_selected": row["approximation_selected"],
        "eligible": row["eligible"],
        "approximation_eligible": row["approximation_eligible"],
        "preset": row["preset"],
        "preset_mapping_tier": row["preset_mapping"]["tier"],
        "preset_mapping_fidelity": row["preset_mapping"]["fidelity"] or "",
        "preset_mapping_reasons": ";".join(
            row["preset_mapping"]["reasons"]
            + row["preset_mapping"]["reported_reasons"]
        ),
        "performance_passed": performance["passed"],
        "performance_reasons": ";".join(performance["reasons"]),
        "mean_ms": "" if performance["mean_ms"] is None else performance["mean_ms"],
        "p95_ms": "" if performance["p95_ms"] is None else performance["p95_ms"],
        "mean_fps": "" if performance["mean_fps"] is None else performance["mean_fps"],
        "p95_fps": "" if performance["p95_fps"] is None else performance["p95_fps"],
        "preview_passed": preview["passed"],
        "preview_reasons": ";".join(preview["reasons"]),
        "visual_integrity_passed": visual["passed"],
        "visual_integrity_reasons": ";".join(visual["reasons"]),
        "mae_rgb": difference.get("mae_rgb", ""),
        "changed_pixel_ratio": difference.get("changed_pixel_ratio", ""),
        "strong_changed_pixel_ratio": difference.get("strong_changed_pixel_ratio", ""),
        "luma_correlation": difference.get("luma_correlation", ""),
        "global_luma_ssim": difference.get("global_luma_ssim", ""),
        "windowed_luma_ssim": difference.get("windowed_luma_ssim", ""),
        "highlight_clipping_ratio": difference.get("highlight_clipping_ratio", ""),
        "effect_presence_score": row.get("effect_presence_score", ""),
        "min_distance_to_earlier": row.get("min_distance_to_earlier", ""),
        "artifact_scale_bucket": perceptual.get("artifact_scale_bucket", ""),
        "artifact_orientation": perceptual.get("artifact_orientation", ""),
        "liveliness_passed": row["liveliness_gate"]["passed"],
        "liveliness_reasons": ";".join(row["liveliness_gate"]["reasons"]),
        "liveliness_lum_mean": liveliness.get("lum_mean", ""),
        "liveliness_occ_soft": liveliness.get("occ_soft", ""),
        "liveliness_blobs": liveliness.get("blobs", ""),
        "liveliness_shape_entropy": liveliness.get("shape_entropy", ""),
        "preview_path": row["preview"]["path"] or "",
        "render_path_fidelity": row["fidelity"]["render_path"],
        "codec_pixel_fidelity": row["fidelity"]["codec_pixel_fidelity"],
    }


def write_csv(path: Path, document: dict[str, Any]) -> None:
    ordered = sorted(
        document["results"],
        key=lambda row: (
            0
            if row["rank"] is not None
            else (1 if row["approximation_rank"] is not None else 2),
            row["rank"]
            if row["rank"] is not None
            else (
                row["approximation_rank"]
                if row["approximation_rank"] is not None
                else 10**9
            ),
            row["preset"].casefold(),
            row["preset"],
        ),
    )

    def writer(handle: TextIO) -> None:
        output = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        output.writeheader()
        for row in ordered:
            output.writerow(_csv_row(row))

    atomic_write(path, writer)


def _relative_image_source(image: str | None, html_path: Path) -> str:
    if not image:
        return ""
    return Path(os.path.relpath(image, html_path.parent)).as_posix()


def render_html(document: dict[str, Any], html_path: Path) -> str:
    summary = document["summary"]
    selected = sorted(
        (row for row in document["results"] if row["selected"]), key=lambda row: row["rank"]
    )
    approximation_selected = sorted(
        (row for row in document["results"] if row["approximation_selected"]),
        key=lambda row: row["approximation_rank"],
    )

    def card(row: dict[str, Any], rank_key: str, tier: str) -> str:
        performance = row["performance_gate"]
        difference = row["difference"] or {}
        perceptual = row["perceptual"] or {}
        source = html.escape(
            _relative_image_source(row["preview"]["path"], html_path), quote=True
        )
        distance = (
            row["min_distance_to_earlier"]
            if row["min_distance_to_earlier"] is not None
            else "seed"
        )
        return (
            "<article class=card>"
            f"<img loading=lazy src=\"{source}\" alt=\"{html.escape(row['preset'], quote=True)}\">"
            f"<div class=body><p class=rank>{html.escape(tier)} #{row[rank_key]}</p>"
            f"<h2>{html.escape(row['preset'])}</h2>"
            f"<p>p95 {performance['p95_ms']:.3f} ms · mean {performance['mean_ms']:.3f} ms</p>"
            f"<p>change {difference.get('changed_pixel_ratio', 0.0):.1%} · "
            f"MAE {difference.get('mae_rgb', 0.0):.2f}</p>"
            f"<p>{html.escape(str(perceptual.get('artifact_scale_bucket', 'unknown')))} / "
            f"{html.escape(str(perceptual.get('artifact_orientation', 'unknown')))} · "
            f"distance {distance}</p></div></article>"
        )

    cards = [card(row, "rank", "compatible") for row in selected]
    approximation_cards = [
        card(row, "approximation_rank", "approximation")
        for row in approximation_selected
    ]

    table_rows: list[str] = []
    ordered = sorted(
        document["results"],
        key=lambda row: (
            0
            if row["rank"] is not None
            else (1 if row["approximation_rank"] is not None else 2),
            row["rank"]
            if row["rank"] is not None
            else (
                row["approximation_rank"]
                if row["approximation_rank"] is not None
                else 10**9
            ),
            row["preset"].casefold(),
        ),
    )
    for row in ordered:
        gate = row["performance_gate"]
        mapping = row["preset_mapping"]
        reasons = (
            mapping["reasons"]
            + mapping["reported_reasons"]
            + gate["reasons"]
            + row["preview_gate"]["reasons"]
            + row["visual_integrity_gate"]["reasons"]
        )
        mean_text = "" if gate["mean_ms"] is None else f"{gate['mean_ms']:.3f}"
        p95_text = "" if gate["p95_ms"] is None else f"{gate['p95_ms']:.3f}"
        rank_text = (
            str(row["rank"])
            if row["rank"] is not None
            else (
                f"A{row['approximation_rank']}"
                if row["approximation_rank"] is not None
                else ""
            )
        )
        table_rows.append(
            "<tr>"
            f"<td>{rank_text}</td>"
            f"<td>{html.escape(row['preset'])}</td>"
            f"<td>{html.escape(mapping['tier'])}</td>"
            f"<td>{'PASS' if gate['passed'] else 'FAIL'}</td>"
            f"<td>{mean_text}</td>"
            f"<td>{p95_text}</td>"
            f"<td>{html.escape(', '.join(reasons))}</td>"
            "</tr>"
        )

    fidelity_label = html.escape(document["fidelity"]["label"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Original GLIC preset realtime audit</title>
<style>
:root{{color-scheme:dark;background:#0a0a0a;color:#eee;font:15px/1.45 system-ui,sans-serif}}
body{{margin:0}}header,main{{max-width:1440px;margin:auto;padding:24px}}header{{border-bottom:1px solid #333}}
.warning{{background:#2a2310;border:1px solid #8c6d1f;padding:14px;border-radius:8px}}
.stats{{display:flex;gap:24px;flex-wrap:wrap}}.stats b{{font-size:1.6rem;display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
.card{{background:#151515;border:1px solid #333;border-radius:10px;overflow:hidden}}.card img{{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}}
.body{{padding:12px}}.body h2{{font-size:1rem;margin:0 0 8px;overflow-wrap:anywhere}}.body p{{margin:4px 0;color:#bbb}}.rank{{color:#fff!important;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}th,td{{padding:8px;border-bottom:1px solid #333;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#111}}
.table-wrap{{overflow:auto;max-height:70vh;border:1px solid #333}}
</style></head><body><header><h1>Original named GLIC presets — realtime audit</h1>
<p class="warning"><strong>Fidelity boundary:</strong> {fidelity_label}</p>
<div class="stats"><p><b>{summary['performance_passed']}</b>30 fps gate passed</p>
<p><b>{summary['mapping_exact_compatible']}</b>exact-compatible mappings</p>
<p><b>{summary['mapping_approximated']}</b>approximated mappings</p>
<p><b>{summary['mapping_unsupported']}</b>unsupported/unreported mappings</p>
<p><b>{summary['eligible']}</b>compatible + eligible</p><p><b>{summary['selected']}</b>compatible selection</p>
<p><b>{summary['approximated_selected']}</b>separate approximation selection</p>
<p><b>{summary['liveliness_valid']}</b>liveliness rows (unscored)</p>
<p><b>{summary['minimum_selected_pair_distance'] if summary['minimum_selected_pair_distance'] is not None else '—'}</b>minimum pair distance</p></div></header>
<main><h2>Original-parameter compatible selection</h2><section class="grid">{''.join(cards)}</section>
<h2>Approximation tier (not original-compatible)</h2><section class="grid">{''.join(approximation_cards)}</section>
<h2>Complete audit</h2><div class="table-wrap"><table><thead><tr><th>Rank</th><th>Preset</th><th>Mapping tier</th><th>30 fps gate</th><th>Mean ms</th><th>p95 ms</th><th>Reasons</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table></div></main></body></html>
"""


def write_html(path: Path, document: dict[str, Any]) -> None:
    absolute = path.expanduser().resolve()

    def writer(handle: TextIO) -> None:
        handle.write(render_html(document, absolute))

    atomic_write(absolute, writer)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    previews = parser.add_mutually_exclusive_group(required=True)
    previews.add_argument("--previews-dir", type=Path)
    previews.add_argument("--preview-manifest", type=Path)
    parser.add_argument("--dry", type=Path, required=True, help="960x540 dry reference PNG")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--liveliness-json",
        type=Path,
        help="optional visual-liveliness array joined by each row's exact name label",
    )
    parser.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_PRESETS)
    parser.add_argument(
        "--select", type=int, default=DEFAULT_SELECTION_COUNT, dest="select_count"
    )
    parser.add_argument(
        "--preset-corpus-manifest",
        type=Path,
        default=DEFAULT_UPSTREAM_PRESET_MANIFEST,
        help="pinned upstream SHA-256 manifest (default: repository manifest)",
    )
    parser.add_argument(
        "--preset-corpus-dir",
        type=Path,
        default=DEFAULT_UPSTREAM_PRESET_DIRECTORY,
        help="local upstream preset corpus to verify (default: repository presets)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        preview_map = (
            discover_previews(args.previews_dir.expanduser().resolve())
            if args.previews_dir is not None
            else load_preview_manifest(args.preview_manifest.expanduser().resolve())
        )
        document = build_catalog(
            args.benchmark.expanduser().resolve(),
            preview_map,
            args.dry.expanduser().resolve(),
            expected_count=args.expected_count,
            select_count=args.select_count,
            liveliness_path=(
                None
                if args.liveliness_json is None
                else args.liveliness_json.expanduser().resolve()
            ),
            corpus_manifest_path=args.preset_corpus_manifest.expanduser().resolve(),
            preset_corpus_dir=args.preset_corpus_dir.expanduser().resolve(),
        )
        output = args.output_dir.expanduser().resolve()
        write_json(output / "ranking.json", document)
        write_csv(output / "ranking.csv", document)
        write_html(output / "index.html", document)
    except CatalogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(
        f"wrote {output}: status={document['status']} "
        f"performance_passed={document['summary']['performance_passed']} "
        f"eligible={document['summary']['eligible']} selected={document['summary']['selected']}"
    )
    return 0 if document["publishable"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
