#!/usr/bin/env python3
"""Create an explainable Pareto/diversity ranking from a GLIC search archive."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote


SCHEMA = "glic-search-ranking-v2"
POLICY_VERSION = "pareto-perceptual-mmr-v2"
PERFORMANCE_CERTIFICATION_SCHEMA = "glic-search-performance-certifications-v1"
PERFORMANCE_POLICY_VERSION = "metal-960x540-p95-30fps-v1"
PERFORMANCE_BACKEND = "metal"
PERFORMANCE_WIDTH = 960
PERFORMANCE_HEIGHT = 540
PERFORMANCE_MIN_WARMUP_FRAMES = 10
PERFORMANCE_MIN_MEASURED_FRAMES = 120
PERFORMANCE_REQUIRED_FPS = 30.0
PERFORMANCE_MAX_P95_MS = 1000.0 / PERFORMANCE_REQUIRED_FPS
RECIPE_SHA256_METHOD = "sha256-utf8-canonical-json-v1"
REQUIRED_METRICS = (
    "mae",
    "changed_ratio",
    "luma_correlation",
    "structure",
    "clipping_ratio",
    "entropy",
    "temporal_residual_delta",
    "content_dependency",
    "output_stddev",
    "min_input_changed_ratio",
    "mean_process_ms",
)
LIVELINESS_METRICS = (
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
)
PERCEPTUAL_SCALARS = (
    "colorfulness",
    "saturation_mean",
    "saturation_std",
    "local_contrast",
    "edge_density",
    "blockiness",
    "channel_separation",
)
PRIMARY_PARETO_FAMILIES = (
    "controlled_damage",
    "source_relation",
    "signal_health",
    "input_robustness",
)
FAMILY_WEIGHTS = {
    "controlled_damage": 0.20,
    "source_relation": 0.20,
    "signal_health": 0.14,
    "input_robustness": 0.18,
    "temporal_control": 0.08,
    "realtime_headroom": 0.03,
    "presence_balance": 0.05,
    "shape_character": 0.06,
    "perceptual_richness": 0.06,
}
TIER_LIMITS = (("finalist", 12), ("shortlist", 32), ("reserve", 64))


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
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class CertificationValidationError(ValueError):
    """The certification sidecar cannot be trusted as a complete snapshot."""


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CertificationValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_nonfinite_json_constant(value: str) -> None:
    raise CertificationValidationError(f"non-finite JSON constant: {value}")


def load_strict_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_json_keys,
        parse_constant=reject_nonfinite_json_constant,
    )


def canonical_recipe_json(recipe: dict[str, Any]) -> str:
    """Canonical recipe identity: sorted UTF-8 JSON with no insignificant whitespace."""
    return json.dumps(
        recipe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_recipe_sha256(recipe: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_recipe_json(recipe).encode("utf-8")).hexdigest()


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def identity_text(value: Any) -> str:
    """Preserve valid zero-valued numeric IDs while rejecting empty identities."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    return str(value)


def first_identity(*values: Any) -> str:
    for value in values:
        text = identity_text(value)
        if text:
            return text
    return ""


def valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def ramp_up(value: float, bad: float, good: float) -> float:
    if good <= bad:
        return 1.0
    return clamp((value - bad) / (good - bad))


def ramp_down(value: float, good: float, bad: float) -> float:
    if bad <= good:
        return 1.0
    return clamp((bad - value) / (bad - good))


def plateau(value: float, bad_low: float, good_low: float, good_high: float, bad_high: float) -> float:
    if value < good_low:
        return ramp_up(value, bad_low, good_low)
    if value <= good_high:
        return 1.0
    return ramp_down(value, good_high, bad_high)


def geometric_mean(values: Iterable[float]) -> float:
    numbers = [clamp(float(value)) for value in values]
    if not numbers or any(value <= 0.0 for value in numbers):
        return 0.0
    return math.exp(sum(math.log(value) for value in numbers) / len(numbers))


def weighted_mean(values: dict[str, float], weights: dict[str, float]) -> float:
    active = [(values[name], weight) for name, weight in weights.items() if name in values and weight > 0]
    denominator = sum(weight for _, weight in active)
    return 0.0 if denominator <= 0 else sum(value * weight for value, weight in active) / denominator


def weighted_geometric_mean(values: dict[str, float], weights: dict[str, float]) -> float:
    active = [(clamp(values[name]), weight) for name, weight in weights.items() if name in values and weight > 0]
    denominator = sum(weight for _, weight in active)
    if denominator <= 0 or any(value <= 0 for value, _ in active):
        return 0.0
    return math.exp(sum(weight * math.log(value) for value, weight in active) / denominator)


def quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = clamp(probability) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def summarize(values: Iterable[Any], total: int) -> dict[str, Any]:
    finite_values = [number for value in values if (number := finite(value)) is not None]
    unique = len({round(value, 10) for value in finite_values})
    result: dict[str, Any] = {
        "coverage": 0.0 if total == 0 else len(finite_values) / total,
        "count": len(finite_values),
        "unique_count": unique,
    }
    for label, probability in (
        ("q00", 0.0),
        ("q05", 0.05),
        ("q10", 0.10),
        ("q25", 0.25),
        ("q50", 0.50),
        ("q75", 0.75),
        ("q90", 0.90),
        ("q95", 0.95),
        ("q100", 1.0),
    ):
        result[label] = quantile(finite_values, probability)
    q05, q95 = result["q05"], result["q95"]
    result["robust_span"] = None if q05 is None or q95 is None else q95 - q05
    return result


def calibrated_plateau(value: float, stats: dict[str, Any], wide: bool = False) -> float:
    low = stats["q00"] if wide else stats["q05"]
    high = stats["q100"] if wide else stats["q95"]
    good_low = stats["q25"]
    good_high = stats["q75"]
    if None in (low, good_low, good_high, high) or high - low <= 1e-12:
        return 1.0
    return plateau(value, low, good_low, good_high, high)


def calibrated_richness(value: float, stats: dict[str, Any]) -> float:
    low, good, high = stats["q05"], stats["q75"], stats["q100"]
    if None in (low, good, high) or high - low <= 1e-12:
        return 1.0
    # Reward a strong characteristic, but taper the most extreme five percent.
    return plateau(value, low, good, stats["q95"], high)


def raw_metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    record = candidate.get("record")
    if not isinstance(record, dict):
        return {}
    metrics = record.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def hard_gate_reasons(metrics: dict[str, Any]) -> list[str]:
    values: dict[str, float] = {}
    reasons: list[str] = []
    for name in REQUIRED_METRICS:
        value = finite(metrics.get(name))
        if value is None:
            reasons.append(f"missing_or_nonfinite:{name}")
        else:
            values[name] = value
    if reasons:
        return reasons
    if values["mean_process_ms"] > 1000.0 / 30.0:
        reasons.append("below_30_fps_lowres_prefilter")
    if values["mean_process_ms"] <= 0.0:
        reasons.append("invalid_process_time")
    if (
        values["mae"] < 8.0
        or values["changed_ratio"] < 0.20
        or values["min_input_changed_ratio"] < 0.15
    ):
        reasons.append("no_op")
    if values["mae"] > 75.0 or values["changed_ratio"] > 0.95:
        reasons.append("excessive_change")
    if values["entropy"] < 0.12 or values["output_stddev"] < 0.031:
        reasons.append("collapsed_output")
    if values["clipping_ratio"] > 0.25:
        reasons.append("excessive_clipping")
    if (
        abs(values["luma_correlation"]) < 0.10
        or values["structure"] < 0.15
        or values["content_dependency"] < 0.15
    ):
        reasons.append("input_independent_noise")
    return reasons


def performance_gate_reasons(certification: dict[str, Any] | None) -> list[str]:
    if not isinstance(certification, dict):
        return ["missing_960x540_performance_certification"]

    reasons: list[str] = []
    status = certification.get("status")
    if status != "passed":
        reasons.append(f"performance_certification_status:{status or 'missing'}")
    if certification.get("backend") != PERFORMANCE_BACKEND:
        reasons.append("performance_backend_mismatch")

    width = finite(certification.get("width"))
    height = finite(certification.get("height"))
    if width != PERFORMANCE_WIDTH or height != PERFORMANCE_HEIGHT:
        reasons.append("performance_resolution_mismatch")

    warmup_frames = finite(certification.get("warmup_frames"))
    measured_frames = finite(certification.get("frames_measured"))
    if warmup_frames is None or warmup_frames < PERFORMANCE_MIN_WARMUP_FRAMES:
        reasons.append("insufficient_performance_warmup_frames")
    if measured_frames is None or measured_frames < PERFORMANCE_MIN_MEASURED_FRAMES:
        reasons.append("insufficient_performance_measured_frames")
    if certification.get("process_passed") is not True:
        reasons.append("performance_process_failed")
    if certification.get("performance_passed") is not True:
        reasons.append("performance_gate_failed")

    mean_ms = finite(certification.get("mean_ms"))
    p95_ms = finite(certification.get("p95_ms"))
    if mean_ms is None or mean_ms <= 0.0 or p95_ms is None or p95_ms <= 0.0:
        reasons.append("invalid_performance_measurement")
    elif mean_ms > PERFORMANCE_MAX_P95_MS or p95_ms > PERFORMANCE_MAX_P95_MS:
        reasons.append("below_30_fps_p95_at_960x540")

    p95_fps = finite(certification.get("p95_fps"))
    expected_p95_fps = None if p95_ms is None or p95_ms <= 0.0 else 1000.0 / p95_ms
    if (
        p95_fps is None
        or p95_fps <= 0.0
        or expected_p95_fps is None
        or not math.isclose(p95_fps, expected_p95_fps, rel_tol=1e-4, abs_tol=1e-3)
    ):
        reasons.append("invalid_certified_p95_fps")
    return reasons


def _certification_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_records = payload.get("records")
    indexed: dict[str, dict[str, Any]] = {}
    if isinstance(raw_records, dict):
        source = raw_records.items()
    elif isinstance(raw_records, list):
        source = ((str(record.get("recipe_hash") or ""), record) for record in raw_records if isinstance(record, dict))
        if any(not isinstance(record, dict) for record in raw_records):
            raise CertificationValidationError("performance certification records must be objects")
    else:
        raise CertificationValidationError("performance certification records are missing")

    for raw_key, record in source:
        key = str(raw_key)
        if not key or not isinstance(record, dict):
            raise CertificationValidationError("performance certification record is invalid")
        if key in indexed:
            raise CertificationValidationError(f"duplicate performance certification row: {key}")
        if str(record.get("recipe_hash") or "") != key:
            raise CertificationValidationError(f"performance certification key mismatch: {key}")
        indexed[key] = record
    return indexed


def validate_performance_certifications(
    payload: Any, candidates: list[dict[str, Any]], expected_archive_sha256: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema") != PERFORMANCE_CERTIFICATION_SCHEMA:
        raise CertificationValidationError("performance certification schema is not supported")

    source = payload.get("source")
    archive_sha256 = source.get("archive_sha256") if isinstance(source, dict) else None
    if archive_sha256 != expected_archive_sha256:
        raise CertificationValidationError("performance certification archive snapshot does not match")
    if not isinstance(source, dict):
        raise CertificationValidationError("performance certification source is missing")
    for name in ("input_sha256", "binary_sha256", "metallib_sha256"):
        if not valid_sha256(source.get(name)):
            raise CertificationValidationError(
                f"performance certification source identity is invalid: {name}"
            )
    hardware = source.get("hardware")
    if not isinstance(hardware, dict) or not valid_sha256(hardware.get("fingerprint")):
        raise CertificationValidationError(
            "performance certification hardware identity is invalid"
        )

    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise CertificationValidationError("performance certification policy is missing")
    if policy.get("version") != PERFORMANCE_POLICY_VERSION:
        raise CertificationValidationError("performance certification policy version is not supported")
    if policy.get("recipe_sha256_method") != RECIPE_SHA256_METHOD:
        raise CertificationValidationError("performance certification recipe identity method is not supported")
    expected_policy_numbers = {
        "width": float(PERFORMANCE_WIDTH),
        "height": float(PERFORMANCE_HEIGHT),
        "warmup_frames": float(PERFORMANCE_MIN_WARMUP_FRAMES),
        "measured_frames": float(PERFORMANCE_MIN_MEASURED_FRAMES),
        "required_fps": PERFORMANCE_REQUIRED_FPS,
        "max_p95_ms": PERFORMANCE_MAX_P95_MS,
    }
    if policy.get("backend") != PERFORMANCE_BACKEND:
        raise CertificationValidationError("performance certification policy backend is not Metal")
    for name, expected in expected_policy_numbers.items():
        value = finite(policy.get(name))
        if value is None or not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-9):
            raise CertificationValidationError(f"performance certification policy mismatch: {name}")

    expected: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise CertificationValidationError("catalog candidate is not an object")
        record = candidate.get("record") if isinstance(candidate.get("record"), dict) else {}
        recipe_hash = first_identity(candidate.get("recipe_hash"), record.get("recipe_hash"))
        candidate_id = first_identity(candidate.get("candidate_id"), record.get("candidate_id"))
        recipe = record.get("recipe") if isinstance(record.get("recipe"), dict) else None
        if not recipe_hash or not candidate_id or recipe is None:
            raise CertificationValidationError("catalog candidate lacks performance identity")
        if recipe_hash in expected:
            raise CertificationValidationError(f"duplicate catalog recipe hash: {recipe_hash}")
        try:
            recipe_sha256 = canonical_recipe_sha256(recipe)
        except (TypeError, ValueError) as error:
            raise CertificationValidationError(
                f"catalog recipe is not canonicalizable: {recipe_hash}: {error}"
            ) from error
        expected[recipe_hash] = (candidate_id, recipe_sha256)

    indexed = _certification_records(payload)
    missing = sorted(set(expected) - set(indexed))
    extra = sorted(set(indexed) - set(expected))
    if missing or extra:
        raise CertificationValidationError(
            f"performance certification row set mismatch: missing={missing[:3]} extra={extra[:3]}"
        )
    for recipe_hash, (candidate_id, recipe_sha256) in expected.items():
        certification = indexed[recipe_hash]
        if identity_text(certification.get("candidate_id")) != candidate_id:
            raise CertificationValidationError(
                f"performance certification candidate identity mismatch: {recipe_hash}"
            )
        if certification.get("recipe_sha256") != recipe_sha256:
            raise CertificationValidationError(
                f"performance certification recipe identity mismatch: {recipe_hash}"
            )
    return indexed


def get_analysis(
    candidate: dict[str, Any], by_recipe: dict[str, dict[str, Any]], by_candidate: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    recipe_hash = identity_text(candidate.get("recipe_hash"))
    candidate_id = identity_text(candidate.get("candidate_id"))
    return by_recipe.get(recipe_hash) or by_candidate.get(candidate_id)


def recipe_family(recipe: dict[str, Any]) -> str:
    channels = recipe.get("channels") if isinstance(recipe, dict) else None
    if not isinstance(channels, list) or not channels:
        return "unknown"
    modes: list[int] = []
    wavelets: list[int] = []
    encodings: list[int] = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        encoding = int(finite(channel.get("encoding")) or 0)
        prediction = int(abs(finite(channel.get("prediction")) or 0))
        wavelet = int(finite(channel.get("wavelet")) or 0)
        modes.append((encoding + prediction) % 6)
        encodings.append(encoding)
        wavelets.append(wavelet)
    if not modes:
        return "unknown"
    mode = Counter(modes).most_common(1)[0][0]
    unique_encodings = len(set(encodings))
    channel_pattern = "uniform" if unique_encodings == 1 else ("dual" if unique_encodings == 2 else "rgb-split")
    nonzero_wavelets = sum(value != 0 for value in wavelets)
    wavelet_pattern = "none" if nonzero_wavelets == 0 else ("all" if nonzero_wavelets == len(wavelets) else "mixed")
    return f"mode{mode}:{channel_pattern}:{wavelet_pattern}"


def prepare_items(
    candidates: list[dict[str, Any]],
    image_analysis: dict[str, Any],
    certifications_by_recipe: dict[str, dict[str, Any]],
    run_dir: Path,
) -> list[dict[str, Any]]:
    records = image_analysis.get("records", []) if isinstance(image_analysis, dict) else []
    by_recipe = {
        str(record.get("recipe_hash")): record
        for record in records
        if isinstance(record, dict) and record.get("recipe_hash")
    }
    by_candidate = {
        str(record.get("candidate_id")): record
        for record in records
        if isinstance(record, dict) and record.get("candidate_id") is not None
    }
    items: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda row: (
            identity_text(row.get("recipe_hash")),
            identity_text(row.get("candidate_id")),
        ),
    ):
        record = candidate.get("record") if isinstance(candidate.get("record"), dict) else {}
        recipe_hash = first_identity(candidate.get("recipe_hash"), record.get("recipe_hash"))
        recipe = record.get("recipe") if isinstance(record.get("recipe"), dict) else {}
        certification = certifications_by_recipe.get(recipe_hash)
        performance_reasons = performance_gate_reasons(certification)
        certification_report = dict(certification) if isinstance(certification, dict) else {}
        certification_report["certified"] = not performance_reasons
        certification_report["gate_reasons"] = performance_reasons
        metrics = raw_metrics(candidate)
        analysis = get_analysis(candidate, by_recipe, by_candidate)
        preview_path = str(
            (analysis.get("preview_path") if isinstance(analysis, dict) else "")
            or candidate.get("preview_path")
            or record.get("preview")
            or ""
        )
        reasons = hard_gate_reasons(metrics) + performance_reasons
        if not recipe_hash:
            reasons.append("missing_recipe_hash")
        if not preview_path:
            reasons.append("missing_preview_path")
        else:
            preview = Path(preview_path).expanduser()
            resolved = preview.resolve() if preview.is_absolute() else (run_dir / preview).resolve()
            try:
                resolved.relative_to(run_dir)
            except ValueError:
                reasons.append("unsafe_preview_path")
            else:
                if not resolved.is_file():
                    reasons.append("missing_preview_file")
        if not isinstance(analysis, dict):
            reasons.append("missing_image_analysis")
            liveliness: dict[str, Any] = {}
            perceptual: dict[str, Any] = {}
        else:
            liveliness = analysis.get("liveliness") if isinstance(analysis.get("liveliness"), dict) else {}
            perceptual = analysis.get("perceptual") if isinstance(analysis.get("perceptual"), dict) else {}
            missing_liveliness = [name for name in LIVELINESS_METRICS if finite(liveliness.get(name)) is None]
            missing_perceptual = [name for name in PERCEPTUAL_SCALARS if finite(perceptual.get(name)) is None]
            for name in ("phash", "dhash", "hsv_hist", "luma_grid", "edge_grid", "color_grid"):
                if name not in perceptual:
                    missing_perceptual.append(name)
            if missing_liveliness:
                reasons.append("incomplete_liveliness:" + ",".join(missing_liveliness))
            if missing_perceptual:
                reasons.append("incomplete_perceptual:" + ",".join(missing_perceptual))
        mean_ms = finite(metrics.get("mean_process_ms"))
        certified_p95_fps = finite(certification_report.get("p95_fps"))
        changed = finite(metrics.get("changed_ratio"))
        minimum_changed = finite(metrics.get("min_input_changed_ratio"))
        derived = {
            "mean_fps": None if mean_ms is None or mean_ms <= 0 else 1000.0 / mean_ms,
            "search_mean_fps": None if mean_ms is None or mean_ms <= 0 else 1000.0 / mean_ms,
            "certified_p95_fps": certified_p95_fps,
            "certified_headroom_ratio": None
            if certified_p95_fps is None
            else certified_p95_fps / PERFORMANCE_REQUIRED_FPS,
            "input_consistency": None
            if changed is None or changed <= 0 or minimum_changed is None
            else clamp(minimum_changed / changed),
            "abs_luma_correlation": None
            if finite(metrics.get("luma_correlation")) is None
            else abs(float(metrics["luma_correlation"])),
            "unclipped": None
            if finite(metrics.get("clipping_ratio")) is None
            else 1.0 - float(metrics["clipping_ratio"]),
        }
        item = {
            "candidate_id": first_identity(
                candidate.get("candidate_id"), record.get("candidate_id")
            ),
            "recipe_hash": recipe_hash,
            "preview_hash": str(record.get("preview_hash") or ""),
            "evaluation_hash": str(record.get("evaluation_hash") or ""),
            "archive_cell": str(candidate.get("archive_cell") or record.get("cell") or ""),
            "generation": str(record.get("generation") or ""),
            "parent_hash": str(record.get("parent_hash") or ""),
            "recipe_family": recipe_family(recipe),
            "color_space": recipe.get("color_space"),
            "eligible": not reasons,
            "hard_gate": {"passed": not reasons, "reasons": reasons},
            "performance_certification": certification_report,
            "raw_metrics": {name: finite(metrics.get(name)) for name in REQUIRED_METRICS},
            "liveliness": {name: finite(liveliness.get(name)) for name in LIVELINESS_METRICS},
            "perceptual": perceptual,
            "derived_metrics": derived,
            "search_quality": finite(candidate.get("quality")),
            "preview_path": preview_path,
            "recipe": recipe,
        }
        items.append(item)

    # Archive code already prevents exact duplicates, but keep the invariant at the report boundary.
    seen: dict[tuple[str, str], str] = {}
    for item in sorted(
        (row for row in items if row["eligible"]),
        key=lambda row: (-(row.get("search_quality") or 0.0), row["recipe_hash"], row["candidate_id"]),
    ):
        keys = [
            ("evaluation_hash", item["evaluation_hash"]),
            ("preview_hash", item["preview_hash"]),
            ("recipe_hash", item["recipe_hash"]),
        ]
        duplicate_of = next((seen[key] for key in keys if key[1] and key in seen), None)
        if duplicate_of:
            item["eligible"] = False
            item["hard_gate"]["passed"] = False
            item["hard_gate"]["reasons"].append(f"exact_duplicate_of:{duplicate_of}")
            continue
        for key in keys:
            if key[1]:
                seen[key] = item["recipe_hash"]
    return items


def raw_calibration(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    eligible = [item for item in items if item["eligible"]]
    calibration: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_METRICS:
        calibration[name] = summarize((item["raw_metrics"].get(name) for item in eligible), len(eligible))
    for name in LIVELINESS_METRICS:
        calibration[name] = summarize((item["liveliness"].get(name) for item in eligible), len(eligible))
    for name in PERCEPTUAL_SCALARS:
        calibration[name] = summarize((item["perceptual"].get(name) for item in eligible), len(eligible))
    calibration["search_mean_fps"] = summarize(
        (item["derived_metrics"].get("search_mean_fps") for item in eligible), len(eligible)
    )
    calibration["certified_p95_fps"] = summarize(
        (item["derived_metrics"].get("certified_p95_fps") for item in eligible), len(eligible)
    )
    return calibration


def score_families(item: dict[str, Any], calibration: dict[str, dict[str, Any]]) -> dict[str, float]:
    metric = item["raw_metrics"]
    live = item["liveliness"]
    visual = item["perceptual"]
    consistency = item["derived_metrics"]["input_consistency"]
    fps = item["derived_metrics"]["certified_p95_fps"]
    controlled = geometric_mean(
        (
            plateau(metric["mae"], 8.0, 25.0, 55.0, 75.0),
            plateau(metric["changed_ratio"], 0.20, 0.45, 0.88, 0.95),
        )
    )
    source = geometric_mean(
        (
            ramp_up(metric["structure"], 0.15, 0.75),
            ramp_up(abs(metric["luma_correlation"]), 0.10, 0.85),
            ramp_up(metric["content_dependency"], 0.15, 0.95),
        )
    )
    signal = geometric_mean(
        (
            ramp_down(metric["clipping_ratio"], 0.08, 0.25),
            plateau(metric["entropy"], 0.12, 0.65, 0.95, 1.0),
            ramp_up(metric["output_stddev"], 0.031, 0.16),
        )
    )
    robustness = geometric_mean(
        (
            ramp_up(metric["min_input_changed_ratio"], 0.15, 0.75),
            ramp_up(consistency, 0.40, 0.90),
        )
    )
    temporal = plateau(metric["temporal_residual_delta"], 0.04, 0.08, 0.18, 0.24)
    performance = ramp_up(fps, PERFORMANCE_REQUIRED_FPS, 60.0)
    presence = geometric_mean(
        (
            calibrated_plateau(live["lum_mean"], calibration["lum_mean"]),
            calibrated_plateau(live["occ_soft"], calibration["occ_soft"]),
        )
    )
    shape = geometric_mean(
        (
            calibrated_richness(live["shape_entropy"], calibration["shape_entropy"]),
            calibrated_plateau(live["area_cv"], calibration["area_cv"]),
            calibrated_plateau(math.log1p(live["blobs"]), log_stats(calibration["blobs"])),
            calibrated_plateau(live["elong_mean"], calibration["elong_mean"]),
            calibrated_plateau(live["solidity"], calibration["solidity"]),
        )
    )
    richness = geometric_mean(
        calibrated_richness(float(visual[name]), calibration[name]) for name in PERCEPTUAL_SCALARS
    )
    return {
        "controlled_damage": controlled,
        "source_relation": source,
        "signal_health": signal,
        "input_robustness": robustness,
        "temporal_control": temporal,
        "realtime_headroom": performance,
        "presence_balance": presence,
        "shape_character": shape,
        "perceptual_richness": richness,
    }


def log_stats(stats: dict[str, Any]) -> dict[str, Any]:
    result = dict(stats)
    for key in ("q00", "q05", "q10", "q25", "q50", "q75", "q90", "q95", "q100"):
        value = finite(stats.get(key))
        result[key] = None if value is None else math.log1p(max(0.0, value))
    if result["q05"] is not None and result["q95"] is not None:
        result["robust_span"] = result["q95"] - result["q05"]
    return result


def family_calibration(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    eligible = [item for item in items if item["eligible"]]
    result: dict[str, dict[str, Any]] = {}
    for name in FAMILY_WEIGHTS:
        stats = summarize((item["family_scores"][name] for item in eligible), len(eligible))
        span = finite(stats.get("robust_span")) or 0.0
        q05 = finite(stats.get("q05")) or 0.0
        if stats["coverage"] < 0.8:
            active, reason = False, "insufficient_coverage"
        elif stats["unique_count"] < 5 or span < 0.02:
            active, reason = False, "low_dynamic_range"
        elif q05 >= 0.98:
            active, reason = False, "saturated"
        else:
            active, reason = True, ""
        stats["active"] = active
        stats["inactive_reason"] = reason
        result[name] = stats
    return result


def quantize(value: float, epsilon: float = 0.05) -> float:
    return clamp(math.floor(value / epsilon + 0.5) * epsilon)


def dominates(left: dict[str, float], right: dict[str, float], axes: Sequence[str]) -> bool:
    return all(left[name] >= right[name] for name in axes) and any(left[name] > right[name] for name in axes)


def nondominated_fronts(items: list[dict[str, Any]], axes: Sequence[str]) -> list[list[dict[str, Any]]]:
    remaining = list(items)
    fronts: list[list[dict[str, Any]]] = []
    while remaining:
        front = [
            candidate
            for candidate in remaining
            if not any(
                other is not candidate
                and dominates(other["pareto_vector"], candidate["pareto_vector"], axes)
                for other in remaining
            )
        ]
        if not front:  # Defensive escape for malformed comparisons.
            front = [min(remaining, key=lambda row: (row["recipe_hash"], row["candidate_id"]))]
        front.sort(key=lambda row: (row["recipe_hash"], row["candidate_id"]))
        for candidate in front:
            remaining.remove(candidate)
        fronts.append(front)
    return fronts


def assign_crowding(front: list[dict[str, Any]], axes: Sequence[str]) -> None:
    for item in front:
        item["crowding_distance"] = 0.0
    if len(front) <= 2:
        for item in front:
            item["crowding_distance"] = None
        return
    for axis in axes:
        ordered = sorted(front, key=lambda row: (row["family_scores"][axis], row["recipe_hash"]))
        low = ordered[0]["family_scores"][axis]
        high = ordered[-1]["family_scores"][axis]
        if high - low <= 1e-12:
            continue
        ordered[0]["crowding_distance"] = None
        ordered[-1]["crowding_distance"] = None
        for index in range(1, len(ordered) - 1):
            if ordered[index]["crowding_distance"] is None:
                continue
            previous_value = ordered[index - 1]["family_scores"][axis]
            next_value = ordered[index + 1]["family_scores"][axis]
            ordered[index]["crowding_distance"] += (next_value - previous_value) / (high - low)


def hex_hamming(left: Any, right: Any) -> float:
    if not isinstance(left, str) or not isinstance(right, str):
        return 1.0
    try:
        bits = max(len(left), len(right)) * 4
        differing = int(left, 16) ^ int(right, 16)
        return bin(differing).count("1") / max(1, bits)
    except ValueError:
        return 1.0


def vector_rmse(left: Any, right: Any) -> float:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right) or not left:
        return 1.0
    pairs = [(finite(a), finite(b)) for a, b in zip(left, right)]
    if any(a is None or b is None for a, b in pairs):
        return 1.0
    return clamp(math.sqrt(sum((a - b) ** 2 for a, b in pairs) / len(pairs)))


def jensen_shannon(left: Any, right: Any) -> float:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right) or not left:
        return 1.0
    a = [max(0.0, finite(value) or 0.0) for value in left]
    b = [max(0.0, finite(value) or 0.0) for value in right]
    sum_a, sum_b = sum(a), sum(b)
    if sum_a <= 0 or sum_b <= 0:
        return 1.0
    a = [value / sum_a for value in a]
    b = [value / sum_b for value in b]
    middle = [(x + y) / 2.0 for x, y in zip(a, b)]

    def divergence(source: list[float]) -> float:
        return sum(value * math.log(value / midpoint, 2) for value, midpoint in zip(source, middle) if value > 0)

    return clamp(math.sqrt(max(0.0, (divergence(a) + divergence(b)) / 2.0)))


def perceptual_components(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float]:
    a, b = left["perceptual"], right["perceptual"]
    return {
        "phash": hex_hamming(a.get("phash"), b.get("phash")),
        "dhash": hex_hamming(a.get("dhash"), b.get("dhash")),
        "hsv": jensen_shannon(a.get("hsv_hist"), b.get("hsv_hist")),
        "luma": vector_rmse(a.get("luma_grid"), b.get("luma_grid")),
        "edge": vector_rmse(a.get("edge_grid"), b.get("edge_grid")),
        "color": vector_rmse(a.get("color_grid"), b.get("color_grid")),
    }


def perceptual_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    component = perceptual_components(left, right)
    return clamp(
        0.18 * component["phash"]
        + 0.14 * component["dhash"]
        + 0.18 * component["hsv"]
        + 0.20 * component["luma"]
        + 0.12 * component["edge"]
        + 0.18 * component["color"]
    )


def is_near_duplicate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("preview_hash") and left["preview_hash"] == right.get("preview_hash"):
        return True
    component = perceptual_components(left, right)
    return (
        component["phash"] <= 6.0 / 64.0
        and component["dhash"] <= 6.0 / 64.0
        and component["hsv"] <= 0.10
        and component["luma"] <= 0.075
        and component["color"] <= 0.10
    ) or perceptual_distance(left, right) <= 0.055


def normalized_metric(value: float, stats: dict[str, Any]) -> float:
    low, high = finite(stats.get("q05")), finite(stats.get("q95"))
    if low is None or high is None or high - low <= 1e-12:
        return 0.5
    return clamp((value - low) / (high - low))


def behavior_distance(left: dict[str, Any], right: dict[str, Any], calibration: dict[str, Any]) -> float:
    values: list[float] = []
    for name in REQUIRED_METRICS[:-1]:  # speed is a gate, not a style distance.
        a, b = left["raw_metrics"][name], right["raw_metrics"][name]
        values.append(abs(normalized_metric(a, calibration[name]) - normalized_metric(b, calibration[name])))
    return sum(values) / len(values)


def recipe_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    a, b = left.get("recipe", {}), right.get("recipe", {})
    values: list[float] = []
    values.append(0.0 if a.get("color_space") == b.get("color_space") else 1.0)
    values.append(clamp(abs((finite(a.get("strength")) or 0.0) - (finite(b.get("strength")) or 0.0))))
    for x, y in zip(a.get("border_rgb", []), b.get("border_rgb", [])):
        values.append(clamp(abs((finite(x) or 0.0) - (finite(y) or 0.0)) / 255.0))
    channel_ranges = {
        "min_block": 32.0,
        "max_block": 32.0,
        "segmentation_precision": 100.0,
        "prediction": 32.0,
        "quantization": 255.0,
        "clamp": 1.0,
        "transform": 1.0,
        "wavelet": 32.0,
        "transform_compress": 255.0,
        "transform_scale": 100.0,
        "encoding": 6.0,
    }
    for channel_a, channel_b in zip(a.get("channels", []), b.get("channels", [])):
        if not isinstance(channel_a, dict) or not isinstance(channel_b, dict):
            continue
        for name, scale in channel_ranges.items():
            values.append(
                clamp(abs((finite(channel_a.get(name)) or 0.0) - (finite(channel_b.get(name)) or 0.0)) / scale)
            )
    return 1.0 if not values else sum(values) / len(values)


def diversity_distance(
    left: dict[str, Any], right: dict[str, Any], calibration: dict[str, Any]
) -> float:
    return clamp(
        0.55 * perceptual_distance(left, right)
        + 0.30 * behavior_distance(left, right, calibration)
        + 0.15 * recipe_distance(left, right)
    )


def merit_key(item: dict[str, Any]) -> tuple[Any, ...]:
    crowding = item.get("crowding_distance")
    crowding_sort = 1e9 if crowding is None else crowding
    return (
        item.get("pareto_front", 10**9),
        -item.get("core_utility", 0.0),
        -crowding_sort,
        -(item.get("search_quality") or 0.0),
        item["recipe_hash"],
        item["candidate_id"],
    )


def assign_clusters(
    eligible: list[dict[str, Any]], calibration: dict[str, Any]
) -> dict[tuple[str, str], float]:
    ordered = sorted(eligible, key=merit_key)
    representatives: list[dict[str, Any]] = []
    distance_cache: dict[tuple[str, str], float] = {}

    def cached_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
        key = tuple(sorted((left["recipe_hash"], right["recipe_hash"])))
        if key not in distance_cache:
            distance_cache[key] = diversity_distance(left, right, calibration)
        return distance_cache[key]

    for candidate in ordered:
        representative = next((row for row in representatives if is_near_duplicate(candidate, row)), None)
        if representative is None:
            representatives.append(candidate)
            candidate["cluster_id"] = f"cluster-{len(representatives):03d}"
            candidate["cluster_representative"] = candidate["recipe_hash"]
            candidate["distance_to_representative"] = 0.0
        else:
            candidate["cluster_id"] = representative["cluster_id"]
            candidate["cluster_representative"] = representative["recipe_hash"]
            candidate["distance_to_representative"] = cached_distance(candidate, representative)
    for candidate in ordered:
        distances = sorted(
            cached_distance(candidate, other) for other in ordered if other is not candidate
        )
        candidate["knn_novelty"] = 1.0 if not distances else sum(distances[:5]) / min(5, len(distances))
    return distance_cache


def tier_config(limit: int) -> tuple[int, int]:
    if limit <= 12:
        return 1, 3
    if limit <= 32:
        return 2, 6
    return 3, 12


def candidate_gain(
    candidate: dict[str, Any],
    selected: list[dict[str, Any]],
    calibration: dict[str, Any],
    distance_cache: dict[tuple[str, str], float],
) -> tuple[float, float]:
    def distance(other: dict[str, Any]) -> float:
        key = tuple(sorted((candidate["recipe_hash"], other["recipe_hash"])))
        if key not in distance_cache:
            distance_cache[key] = diversity_distance(candidate, other, calibration)
        return distance_cache[key]

    nearest = 1.0 if not selected else min(distance(other) for other in selected)
    pareto_bonus = 1.0 / (1.0 + candidate["pareto_front"])
    families = {item["recipe_family"] for item in selected}
    cells = {item["archive_cell"] for item in selected}
    family_bonus = 1.0 if candidate["recipe_family"] not in families else 0.0
    cell_bonus = 1.0 if candidate["archive_cell"] not in cells else 0.0
    gain = (
        0.45 * candidate["core_utility"]
        + 0.15 * pareto_bonus
        + 0.30 * nearest
        + 0.05 * family_bonus
        + 0.05 * cell_bonus
    )
    return gain, nearest


def select_tiers(
    eligible: list[dict[str, Any]],
    calibration: dict[str, Any],
    distance_cache: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_hashes: set[str] = set()
    for _, target in TIER_LIMITS:
        target = min(target, len(eligible))
        cell_limit, family_limit = tier_config(target)
        while len(selected) < target:
            chosen: tuple[dict[str, Any], float, float, int] | None = None
            for relaxation in range(4):
                cell_counts = Counter(item["archive_cell"] for item in selected)
                family_counts = Counter(item["recipe_family"] for item in selected)
                options: list[tuple[dict[str, Any], float, float, int]] = []
                for candidate in eligible:
                    if candidate["recipe_hash"] in selected_hashes:
                        continue
                    if relaxation < 3 and candidate["cluster_representative"] != candidate["recipe_hash"]:
                        continue
                    if relaxation < 1 and family_counts[candidate["recipe_family"]] >= family_limit:
                        continue
                    if relaxation < 2 and cell_counts[candidate["archive_cell"]] >= cell_limit:
                        continue
                    gain, nearest = candidate_gain(candidate, selected, calibration, distance_cache)
                    options.append((candidate, gain, nearest, relaxation))
                if options:
                    options.sort(
                        key=lambda entry: (
                            -entry[1],
                            entry[0]["pareto_front"],
                            -entry[0]["core_utility"],
                            entry[0]["recipe_hash"],
                            entry[0]["candidate_id"],
                        )
                    )
                    chosen = options[0]
                    break
            if chosen is None:
                break
            candidate, gain, nearest, relaxation = chosen
            candidate["selection"] = {
                "gain": gain,
                "nearest_selected_distance": nearest,
                "quota_relaxation_level": relaxation,
            }
            selected.append(candidate)
            selected_hashes.add(candidate["recipe_hash"])
    return selected


def explain(item: dict[str, Any], active_families: Sequence[str]) -> None:
    ordered = sorted(
        ((name, item["family_scores"][name]) for name in active_families),
        key=lambda pair: (-pair[1], pair[0]),
    )
    item["strengths"] = [name for name, score in ordered[:2] if score >= 0.55]
    item["warnings"] = [f"low_{name}" for name, score in ordered[-2:] if score < 0.45]


def diversity_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    distances = [
        perceptual_distance(left, right)
        for index, left in enumerate(items)
        for right in items[index + 1 :]
    ]
    return {
        "count": len(items),
        "pairwise_perceptual_min": min(distances) if distances else None,
        "pairwise_perceptual_median": quantile(distances, 0.5),
        "pairwise_perceptual_mean": None if not distances else sum(distances) / len(distances),
        "unique_cells": len({item["archive_cell"] for item in items}),
        "unique_recipe_families": len({item["recipe_family"] for item in items}),
        "unique_clusters": len({item["cluster_id"] for item in items}),
    }


def diversity_coverage(items: list[dict[str, Any]]) -> dict[str, Any]:
    cluster_counts = Counter(item["cluster_id"] for item in items)
    cluster_representatives = {
        item["cluster_id"]: item["cluster_representative"] for item in items
    }

    def grouped(field: str) -> list[dict[str, Any]]:
        names = sorted({item[field] for item in items})
        rows = []
        for name in names:
            members = [item for item in items if item[field] == name]
            clusters = {item["cluster_id"] for item in members}
            rows.append(
                {
                    "name": name,
                    "candidates": len(members),
                    "unique_clusters": len(clusters),
                    "candidates_per_cluster": len(members) / max(1, len(clusters)),
                }
            )
        return rows

    cells = grouped("archive_cell")
    families = grouped("recipe_family")
    overcrowded = [
        {
            "cluster_id": cluster_id,
            "representative_recipe_hash": cluster_representatives[cluster_id],
            "members": count,
        }
        for cluster_id, count in sorted(
            cluster_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )
        if count > 1
    ][:16]
    target_cells = sorted(
        cells, key=lambda row: (row["unique_clusters"], row["candidates"], row["name"])
    )[:16]
    repetitive_families = sorted(
        families,
        key=lambda row: (-row["candidates_per_cluster"], -row["candidates"], row["name"]),
    )[:16]
    cluster_count = len(cluster_counts)
    candidate_count = len(items)
    return {
        "candidate_count": candidate_count,
        "cluster_count": cluster_count,
        "near_duplicate_compression_ratio": 0.0
        if candidate_count == 0
        else 1.0 - cluster_count / candidate_count,
        "cluster_size": summarize(cluster_counts.values(), cluster_count),
        "archive_cells": cells,
        "recipe_families": families,
        "generation_directives": {
            "policy": "increase global restarts and macro mutations toward undercovered cells; avoid overproducing crowded visual clusters",
            "undercovered_archive_cells": target_cells,
            "overcrowded_visual_clusters": overcrowded,
            "repetitive_recipe_families": repetitive_families,
        },
    }


def rank_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    calibration = raw_calibration(items)
    eligible = [item for item in items if item["eligible"]]
    for item in eligible:
        item["family_scores"] = score_families(item, calibration)
    families = family_calibration(items)
    active_families = [name for name, stats in families.items() if stats["active"]]
    active_weights = {name: FAMILY_WEIGHTS[name] for name in active_families}
    for item in eligible:
        scores = {name: item["family_scores"][name] for name in active_families}
        item["core_utility"] = (
            0.45 * weighted_mean(scores, active_weights)
            + 0.55 * weighted_geometric_mean(scores, active_weights)
        )
    pareto_axes = [name for name in PRIMARY_PARETO_FAMILIES if families[name]["active"]]
    if not pareto_axes and active_families:
        pareto_axes = active_families[:1]
    for item in eligible:
        item["pareto_vector"] = {name: quantize(item["family_scores"][name]) for name in pareto_axes}
    fronts = nondominated_fronts(eligible, pareto_axes) if eligible else []
    for index, front in enumerate(fronts):
        for item in front:
            item["pareto_front"] = index
        assign_crowding(front, pareto_axes)
    distance_cache = assign_clusters(eligible, calibration) if eligible else {}
    selected = select_tiers(eligible, calibration, distance_cache)
    selected_hashes = {item["recipe_hash"] for item in selected}
    representatives = sorted(
        (
            item
            for item in eligible
            if item["recipe_hash"] not in selected_hashes
            and item["cluster_representative"] == item["recipe_hash"]
        ),
        key=merit_key,
    )
    representative_hashes = {item["recipe_hash"] for item in representatives}
    members = sorted(
        (
            item
            for item in eligible
            if item["recipe_hash"] not in selected_hashes and item["recipe_hash"] not in representative_hashes
        ),
        key=lambda row: (row["cluster_id"], merit_key(row)),
    )
    ranked = selected + representatives + members
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
        item["tier"] = "full"
        for tier_name, limit in TIER_LIMITS:
            if index <= min(limit, len(selected)):
                item["tier"] = tier_name
                break
        explain(item, active_families)
    for item in items:
        if not item["eligible"]:
            item["rank"] = None
            item["tier"] = "excluded"
            item["family_scores"] = {}
            item["core_utility"] = None
            item["pareto_vector"] = {}
            item["pareto_front"] = None
            item["crowding_distance"] = None
            item["strengths"] = []
            item["warnings"] = item["hard_gate"]["reasons"]
    selected_top12 = selected[:12]
    legacy_top12 = sorted(
        eligible,
        key=lambda row: (
            -(row.get("search_quality") or 0.0),
            row["recipe_hash"],
            row["candidate_id"],
        ),
    )[:12]
    selected_diversity = diversity_summary(selected_top12)
    legacy_diversity = diversity_summary(legacy_top12)
    coverage = diversity_coverage(eligible)
    selected_median = finite(selected_diversity["pairwise_perceptual_median"])
    legacy_median = finite(legacy_diversity["pairwise_perceptual_median"])
    median_improvement = (
        None
        if selected_median is None or legacy_median is None or legacy_median <= 0
        else selected_median / legacy_median - 1.0
    )
    exclusive_counts = Counter(item["tier"] for item in items)
    certification_status_counts = Counter(
        str(item.get("performance_certification", {}).get("status") or "missing")
        for item in items
    )
    performance_certified = sum(
        item.get("performance_certification", {}).get("certified") is True for item in items
    )
    selected_count = len(selected)
    metadata = {
        "raw_metrics": calibration,
        "families": families,
        "active_families": active_families,
        "pareto_axes": pareto_axes,
        "counts": {
            "input": len(items),
            "eligible": len(eligible),
            "excluded": len(items) - len(eligible),
            "performance_certified": performance_certified,
            "performance_excluded": len(items) - performance_certified,
            "performance_status": dict(sorted(certification_status_counts.items())),
            "clusters": len({item["cluster_id"] for item in eligible}),
            "finalist": min(12, selected_count),
            "shortlist": min(32, selected_count),
            "reserve": min(64, selected_count),
            "tier_additional": {
                "finalist": exclusive_counts["finalist"],
                "shortlist": exclusive_counts["shortlist"],
                "reserve": exclusive_counts["reserve"],
            },
        },
        "baseline_comparison": {
            "selected_top12": selected_diversity,
            "legacy_quality_top12": legacy_diversity,
            "pairwise_median_improvement_ratio": median_improvement,
        },
        "diversity_coverage": coverage,
    }
    return ranked + [item for item in items if not item["eligible"]], metadata


def media_url(raw_path: str, output_dir: Path, media_root: Path) -> str | None:
    if not raw_path or "://" in raw_path or raw_path.startswith("javascript:"):
        return None
    path = Path(raw_path).expanduser()
    resolved = path.resolve() if path.is_absolute() else (media_root / path).resolve()
    try:
        resolved.relative_to(media_root.resolve())
    except ValueError:
        return None
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    relative = Path(os.path.relpath(resolved, output_dir.resolve()))
    return "/".join(quote(component) for component in relative.parts if component != ".")


def build_html(payload: dict[str, Any], output_dir: Path, media_root: Path) -> str:
    metadata = payload["metadata"]
    cards: list[str] = []
    visible = [item for item in payload["candidates"] if item.get("rank") and item["rank"] <= 64]
    for item in visible:
        media = media_url(item.get("preview_path", ""), output_dir, media_root)
        image = '<div class="media empty">No preview</div>'
        if media:
            image = f'<img class="media" src="{html.escape(media, quote=True)}" loading="lazy" alt="Preset preview">'
        family_rows = "".join(
            f'<div class="metric"><span>{html.escape(name.replace("_", " "))}</span>'
            f'<i><b style="width:{clamp(score) * 100:.1f}%"></b></i><em>{score:.2f}</em></div>'
            for name, score in item["family_scores"].items()
        )
        certification = item.get("performance_certification", {})
        p95_ms = finite(certification.get("p95_ms"))
        p95_fps = finite(certification.get("p95_fps"))
        measured_frames = finite(certification.get("frames_measured"))
        certification_text = (
            f'{html.escape(str(certification.get("backend") or "unknown"))} · '
            f'{html.escape(str(certification.get("width") or "?"))}×'
            f'{html.escape(str(certification.get("height") or "?"))} · '
            f'p95 {"—" if p95_ms is None else f"{p95_ms:.3f} ms"} · '
            f'{"—" if p95_fps is None else f"{p95_fps:.1f} fps"} · '
            f'{"—" if measured_frames is None else f"{int(measured_frames)} frames"}'
        )
        cards.append(
            f'''<article class="card tier-{html.escape(item["tier"])}">
              {image}<div class="body">
              <div class="eyebrow">#{item["rank"]} · {html.escape(item["tier"])} · Pareto {item["pareto_front"]}</div>
              <h2>{html.escape(item["recipe_hash"])}</h2>
              <p>{html.escape(item["archive_cell"])} · {html.escape(item["recipe_family"])} · {html.escape(item["cluster_id"])}</p>
              <div class="certification">CERTIFIED · {certification_text}</div>
              <div class="utility">balanced utility <strong>{item["core_utility"]:.3f}</strong></div>
              <div class="metrics">{family_rows}</div>
              </div></article>'''
        )
    inactive = [name for name, stats in metadata["calibration"]["families"].items() if not stats["active"]]
    improvement = finite(
        metadata.get("baseline_comparison", {}).get("pairwise_median_improvement_ratio")
    )
    improvement_text = "n/a" if improvement is None else f"{improvement * 100:+.1f}%"
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GLIC ranked presets</title>
<style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}*{{box-sizing:border-box}}
body{{margin:0;background:#070707;color:#eee}}header{{position:sticky;top:0;z-index:2;padding:18px 26px;background:#090909ef;border-bottom:1px solid #292929}}h1{{margin:0 0 5px;font-size:23px}}header p,.body p{{margin:0;color:#999}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px;padding:20px}}.card{{overflow:hidden;background:#111;border:1px solid #292929;border-radius:11px}}.tier-finalist{{border-color:#69ea8b}}.media{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#030303}}.media.empty{{display:grid;place-items:center;color:#555}}.body{{padding:15px}}.eyebrow{{color:#78ef96;font:12px ui-monospace,monospace;text-transform:uppercase}}h2{{margin:7px 0;font:600 15px ui-monospace,monospace}}.certification{{margin-top:10px;padding:7px 8px;border:1px solid #28643a;border-radius:6px;color:#7bf09a;background:#102417;font:11px ui-monospace,monospace;text-transform:uppercase}}.utility{{margin:13px 0 9px;color:#aaa}}.utility strong{{float:right;color:#fff}}.metric{{display:grid;grid-template-columns:125px 1fr 35px;gap:7px;align-items:center;margin:5px 0;font-size:11px;color:#aaa}}.metric i{{height:5px;background:#282828;border-radius:5px;overflow:hidden}}.metric b{{display:block;height:100%;background:#71d98b}}.metric em{{font-style:normal;text-align:right;font-family:ui-monospace,monospace}}
</style></head><body><header><h1>GLIC Metal ranked presets</h1>
<p>{metadata["counts"]["input"]}候補 / {metadata["counts"]["performance_certified"]}件 960×540 Metal 30fps認証 / {metadata["counts"]["performance_excluded"]}件 性能除外 → {metadata["counts"]["clusters"]}知覚クラスタ → Top 12 / 32 / 64 · p95 ≤ {PERFORMANCE_MAX_P95_MS:.3f} ms · Top 12 median diversity {improvement_text} vs legacy quality順 · inactive: {html.escape(", ".join(inactive) or "none")} · {html.escape(metadata["generated_at"])}</p></header>
<main>{''.join(cards) or '<p>No eligible candidates.</p>'}</main></body></html>'''


def write_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    fields = [
        "rank",
        "tier",
        "candidate_id",
        "recipe_hash",
        "archive_cell",
        "recipe_family",
        "cluster_id",
        "pareto_front",
        "core_utility",
        *FAMILY_WEIGHTS.keys(),
        "knn_novelty",
        "preview_path",
        "certified",
        "certification_status",
        "cert_backend",
        "cert_width",
        "cert_height",
        "cert_warmup_frames",
        "cert_frames",
        "cert_mean_ms",
        "cert_median_ms",
        "cert_p95_ms",
        "cert_p95_fps",
        "cert_p99_ms",
        "cert_max_ms",
        "eligible",
        "exclusion_reasons",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in candidates:
                row = {name: item.get(name, "") for name in fields}
                for name in FAMILY_WEIGHTS:
                    row[name] = item.get("family_scores", {}).get(name, "")
                certification = item.get("performance_certification", {})
                certification_fields = {
                    "certified": certification.get("certified", False),
                    "certification_status": certification.get("status", "missing"),
                    "cert_backend": certification.get("backend", ""),
                    "cert_width": certification.get("width", ""),
                    "cert_height": certification.get("height", ""),
                    "cert_warmup_frames": certification.get("warmup_frames", ""),
                    "cert_frames": certification.get("frames_measured", ""),
                    "cert_mean_ms": certification.get("mean_ms", ""),
                    "cert_median_ms": certification.get("median_ms", ""),
                    "cert_p95_ms": certification.get("p95_ms", ""),
                    "cert_p95_fps": certification.get("p95_fps", ""),
                    "cert_p99_ms": certification.get("p99_ms", ""),
                    "cert_max_ms": certification.get("max_ms", ""),
                }
                row.update(certification_fields)
                row["exclusion_reasons"] = ";".join(item["hard_gate"]["reasons"])
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sanitized_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "rank",
        "tier",
        "candidate_id",
        "recipe_hash",
        "preview_hash",
        "evaluation_hash",
        "archive_cell",
        "generation",
        "parent_hash",
        "recipe_family",
        "color_space",
        "eligible",
        "hard_gate",
        "performance_certification",
        "raw_metrics",
        "liveliness",
        "perceptual",
        "derived_metrics",
        "family_scores",
        "pareto_vector",
        "pareto_front",
        "crowding_distance",
        "search_quality",
        "core_utility",
        "cluster_id",
        "cluster_representative",
        "distance_to_representative",
        "knn_novelty",
        "selection",
        "strengths",
        "warnings",
        "preview_path",
        "recipe",
    }
    return {key: value for key, value in item.items() if key in allowed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--image-analysis", type=Path)
    parser.add_argument("--performance-certifications", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    catalog_path = (args.catalog or run_dir / "catalog.json").expanduser().resolve()
    analysis_path = (args.image_analysis or run_dir / "image-analysis.json").expanduser().resolve()
    certifications_path = (
        args.performance_certifications or run_dir / "performance-certifications.json"
    ).expanduser().resolve()
    output_dir = (args.output_dir or run_dir).expanduser().resolve()
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        image_analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        performance_certifications = load_strict_json(certifications_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"ranking input is not readable: {error}", file=sys.stderr)
        return 2
    except CertificationValidationError as error:
        print(f"performance certification input is invalid: {error}", file=sys.stderr)
        return 2
    candidates = catalog.get("candidates") if isinstance(catalog, dict) else None
    if not isinstance(candidates, list):
        print("catalog.json has no candidate array", file=sys.stderr)
        return 2
    if image_analysis.get("schema") != "glic-search-image-analysis-v1":
        print("image-analysis.json schema is not supported", file=sys.stderr)
        return 2
    archive_path_value = catalog.get("metadata", {}).get("archive_path")
    analysis_archive_sha = image_analysis.get("source", {}).get("archive_sha256")
    if not isinstance(archive_path_value, str) or not isinstance(analysis_archive_sha, str):
        print("ranking inputs do not identify their archive snapshot", file=sys.stderr)
        return 2
    catalog_archive_path = Path(archive_path_value).expanduser().resolve()
    try:
        catalog_archive_sha = sha256_file(catalog_archive_path)
    except OSError as error:
        print(f"catalog archive snapshot is not readable: {error}", file=sys.stderr)
        return 2
    if catalog_archive_sha != analysis_archive_sha:
        print("catalog and image analysis came from different archive snapshots", file=sys.stderr)
        return 3

    try:
        certifications_by_recipe = validate_performance_certifications(
            performance_certifications, candidates, analysis_archive_sha
        )
    except CertificationValidationError as error:
        print(f"performance certification validation failed: {error}", file=sys.stderr)
        return 3

    items = prepare_items(candidates, image_analysis, certifications_by_recipe, run_dir)
    ranked, ranking_metadata = rank_items(items)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "generated_at": generated_at,
        "source": {
            "catalog": str(catalog_path),
            "catalog_generated_at": catalog.get("metadata", {}).get("generated_at"),
            "image_analysis": str(analysis_path),
            "image_analysis_generated_at": image_analysis.get("generated_at"),
            "performance_certifications": str(certifications_path),
            "performance_certifications_generated_at": performance_certifications.get("generated_at"),
            "archive_sha256": image_analysis.get("source", {}).get("archive_sha256"),
            "backend": catalog.get("metadata", {}).get("backend"),
            "width": catalog.get("metadata", {}).get("render_width"),
            "height": catalog.get("metadata", {}).get("render_height"),
            "render_scale": catalog.get("metadata", {}).get("render_scale"),
        },
        "counts": ranking_metadata["counts"],
        "performance_certification": {
            "schema": performance_certifications.get("schema"),
            "policy": performance_certifications.get("policy"),
            "source": performance_certifications.get("source"),
            "recipe_sha256_method": RECIPE_SHA256_METHOD,
        },
        "calibration": {
            "raw_metrics": ranking_metadata["raw_metrics"],
            "families": ranking_metadata["families"],
        },
        "active_families": ranking_metadata["active_families"],
        "pareto_axes": ranking_metadata["pareto_axes"],
        "baseline_comparison": ranking_metadata["baseline_comparison"],
        "diversity_coverage": ranking_metadata["diversity_coverage"],
        "tiers": {"finalist": 12, "shortlist": 32, "reserve": 64},
        "warnings": [
            "temporal_residual_delta is a frame-residual activity metric, not optical flow",
            "presence and shape metrics come from one representative still per preset",
            "performance certification covers synchronous effect processing on the recorded host, not decode, display, or encode",
            "ranking is deterministic technical/perceptual triage, not a learned aesthetic judgment",
        ],
    }
    clean_candidates = [sanitized_item(item) for item in ranked]
    payload = {"schema": SCHEMA, "metadata": metadata, "candidates": clean_candidates}
    shortlist = {
        "schema": SCHEMA,
        "metadata": metadata,
        "candidates": [item for item in clean_candidates if item.get("rank") and item["rank"] <= 32],
    }
    selection = {
        "schema": SCHEMA,
        "metadata": metadata,
        "candidates": [item for item in clean_candidates if item.get("rank") and item["rank"] <= 64],
    }
    generation_directives = {
        "schema": "glic-search-generation-directives-v1",
        "generated_at": generated_at,
        "archive_sha256": metadata["source"]["archive_sha256"],
        "policy_version": POLICY_VERSION,
        "diversity_coverage": metadata["diversity_coverage"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        output_dir / "ranking.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    atomic_write_text(
        output_dir / "shortlist.json",
        json.dumps(shortlist, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    atomic_write_text(
        output_dir / "selection.json",
        json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    atomic_write_text(
        output_dir / "generation-directives.json",
        json.dumps(
            generation_directives,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )
    write_csv(output_dir / "ranking.csv", clean_candidates)
    atomic_write_text(output_dir / "ranking.html", build_html(payload, output_dir, run_dir))
    counts = metadata["counts"]
    print(
        f"ranked {counts['eligible']} eligible candidates into {counts['clusters']} clusters; "
        f"wrote Top 12/32/64 reports to {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
