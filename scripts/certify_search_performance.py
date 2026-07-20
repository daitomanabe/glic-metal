#!/usr/bin/env python3
"""Certify an immutable search archive at Metal 960x540 / 30 fps.

The C++ certifier is the timing authority.  This adapter resolves the exact
canonical recipe for every archive elite, runs that authority in one batch,
validates one result per requested recipe, and only then atomically publishes a
complete certification sidecar.  Failed performance gates are valid complete
records; malformed or incomplete instrumentation fails closed and preserves the
previous sidecar.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform as platform_module
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "glic-search-performance-certifications-v1"
TOOL_RESULT_SCHEMA = "glic-realtime-certification-result-v1"
RECIPE_SHA256_METHOD = "sha256-utf8-canonical-json-v1"
WIDTH = 960
HEIGHT = 540
WARMUP_FRAMES = 10
MEASURED_FRAMES = 120
REQUIRED_FPS = 30.0
MAX_P95_MS = 1000.0 / REQUIRED_FPS
POLICY = {
    "version": "metal-960x540-p95-30fps-v1",
    "backend": "metal",
    "width": WIDTH,
    "height": HEIGHT,
    "warmup_frames": WARMUP_FRAMES,
    "measured_frames": MEASURED_FRAMES,
    "required_fps": REQUIRED_FPS,
    "max_p95_ms": MAX_P95_MS,
    "recipe_sha256_method": RECIPE_SHA256_METHOD,
}


class CertificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    candidate_id: Any
    recipe_hash: str
    recipe: dict[str, Any]
    recipe_sha256: str
    canonical: str = ""
    canonical_sha256: str = ""
    cache_key: str = ""


ACTIVE_PROCESS: subprocess.Popen[str] | None = None


def terminate_process_group(
    process: subprocess.Popen[str], grace_seconds: float = 5.0
) -> None:
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


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CertificationError(f"value cannot be canonicalized as JSON: {error}") from error
    return text.encode("utf-8")


def recipe_sha256(recipe: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(recipe))


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CertificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_nonfinite_constant(value: str) -> Any:
    raise CertificationError(f"non-finite JSON number: {value}")


def strict_json_loads(value: str | bytes, description: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_constant,
        )
    except (CertificationError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CertificationError(f"{description} is not strict JSON: {error}") from error


def valid_recipe_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 16
        and all(character in "0123456789abcdef" for character in value)
    )


def fnv1a64(value: str) -> str:
    result = 1469598103934665603
    for byte in value.encode("utf-8"):
        result ^= byte
        result = (result * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{result:016x}"


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def extract_targets(archive: Any) -> list[Target]:
    if not isinstance(archive, dict):
        raise CertificationError("ranking archive root must be an object")
    if archive.get("schema") not in (
        "glic-realtime-search-archive-v1",
        "glic-realtime-search-archive-v2",
    ):
        raise CertificationError("ranking archive schema is not supported")
    cells = archive.get("cells")
    if not isinstance(cells, dict):
        raise CertificationError("ranking archive has no cells object")

    targets: list[Target] = []
    seen: set[str] = set()
    for cell_name in sorted(cells):
        elites = cells[cell_name]
        if not isinstance(cell_name, str) or not isinstance(elites, list):
            raise CertificationError("ranking archive cells are malformed")
        for index, elite in enumerate(elites):
            if not isinstance(elite, dict):
                raise CertificationError(f"archive elite {cell_name}[{index}] is not an object")
            recipe_hash = elite.get("recipe_hash")
            candidate_id = elite.get("candidate_id")
            recipe = elite.get("recipe")
            if not valid_recipe_hash(recipe_hash):
                raise CertificationError(f"archive elite {cell_name}[{index}] has invalid recipe_hash")
            if candidate_id in (None, "") or isinstance(candidate_id, bool):
                raise CertificationError(f"archive elite {recipe_hash} has no candidate_id")
            if not isinstance(recipe, dict):
                raise CertificationError(f"archive elite {recipe_hash} has no recipe object")
            if recipe_hash in seen:
                raise CertificationError(f"archive contains duplicate recipe hash {recipe_hash}")
            seen.add(recipe_hash)
            targets.append(
                Target(
                    candidate_id=candidate_id,
                    recipe_hash=recipe_hash,
                    recipe=recipe,
                    recipe_sha256=recipe_sha256(recipe),
                )
            )
    if not targets:
        raise CertificationError("ranking archive contains no elites")
    targets.sort(key=lambda target: target.recipe_hash)
    return targets


def resolve_canonicals(candidates_path: Path, targets: list[Target]) -> list[Target]:
    expected = {(target.recipe_hash, str(target.candidate_id)): target for target in targets}
    resolved: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    try:
        handle = candidates_path.open("rb")
    except OSError as error:
        raise CertificationError(f"cannot open candidates log: {error}") from error
    with handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.endswith(b"\n"):
                raise CertificationError(f"candidates log has an incomplete line {line_number}")
            if not raw_line.strip():
                raise CertificationError(f"candidates log has a blank line {line_number}")
            record = strict_json_loads(raw_line, f"candidates line {line_number}")
            if not isinstance(record, dict):
                raise CertificationError(f"candidates line {line_number} is not an object")
            recipe_hash = record.get("recipe_hash")
            candidate_id = record.get("candidate_id")
            key = (str(recipe_hash), str(candidate_id))
            if key not in expected:
                continue
            canonical = record.get("canonical")
            recipe = record.get("recipe")
            if not isinstance(canonical, str) or not canonical or any(
                character in canonical for character in ("\t", "\r", "\n")
            ):
                raise CertificationError(
                    f"candidate {recipe_hash}/{candidate_id} has invalid canonical recipe"
                )
            if fnv1a64(canonical) != recipe_hash:
                raise CertificationError(
                    f"candidate {recipe_hash}/{candidate_id} canonical hash mismatch"
                )
            if not isinstance(recipe, dict):
                raise CertificationError(
                    f"candidate {recipe_hash}/{candidate_id} has no recipe object"
                )
            target = expected[key]
            if recipe_sha256(recipe) != target.recipe_sha256:
                raise CertificationError(
                    f"candidate and archive recipe disagree for {recipe_hash}/{candidate_id}"
                )
            prior = resolved.get(key)
            if prior is not None and prior != (canonical, recipe):
                raise CertificationError(
                    f"conflicting candidate records for {recipe_hash}/{candidate_id}"
                )
            resolved[key] = (canonical, recipe)

    missing = sorted(set(expected) - set(resolved))
    if missing:
        sample = ", ".join(f"{recipe}/{candidate}" for recipe, candidate in missing[:3])
        raise CertificationError(f"archive canonical recipes are missing from candidates log: {sample}")

    result = []
    for target in targets:
        canonical, _recipe = resolved[(target.recipe_hash, str(target.candidate_id))]
        result.append(
            Target(
                candidate_id=target.candidate_id,
                recipe_hash=target.recipe_hash,
                recipe=target.recipe,
                recipe_sha256=target.recipe_sha256,
                canonical=canonical,
                canonical_sha256=sha256_bytes(canonical.encode("utf-8")),
            )
        )
    return result


def make_cache_key(
    target: Target,
    binary_sha256: str,
    metallib_sha256: str,
    input_sha256: str,
    hardware_fingerprint: str,
) -> str:
    identity = {
        "binary_sha256": binary_sha256,
        "metallib_sha256": metallib_sha256,
        "input_sha256": input_sha256,
        "hardware_fingerprint": hardware_fingerprint,
        "policy": POLICY,
        "recipe_hash": target.recipe_hash,
        "recipe_sha256": target.recipe_sha256,
        "canonical_sha256": target.canonical_sha256,
    }
    return sha256_bytes(canonical_json_bytes(identity))


def with_cache_keys(
    targets: Iterable[Target],
    binary_sha256: str,
    metallib_sha256: str,
    input_sha256: str,
    hardware_fingerprint: str,
) -> list[Target]:
    return [
        Target(
            candidate_id=target.candidate_id,
            recipe_hash=target.recipe_hash,
            recipe=target.recipe,
            recipe_sha256=target.recipe_sha256,
            canonical=target.canonical,
            canonical_sha256=target.canonical_sha256,
            cache_key=make_cache_key(
                target,
                binary_sha256,
                metallib_sha256,
                input_sha256,
                hardware_fingerprint,
            ),
        )
        for target in targets
    ]


def fps_consistent(p95_ms: float, p95_fps: float) -> bool:
    expected = 1000.0 / p95_ms
    tolerance = max(1e-7, abs(expected) * 1e-9)
    return abs(expected - p95_fps) <= tolerance


def validate_tool_row(row: Any, expected_hash: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise CertificationError(f"certifier result for {expected_hash} is not an object")
    if row.get("schema") != TOOL_RESULT_SCHEMA or row.get("recipe_hash") != expected_hash:
        raise CertificationError(f"certifier result identity mismatch for {expected_hash}")
    if row.get("backend") != "metal":
        raise CertificationError(f"certifier did not use Metal for {expected_hash}")
    expected_integers = {
        "width": WIDTH,
        "height": HEIGHT,
        "warmup_frames": WARMUP_FRAMES,
        "measured_frames": MEASURED_FRAMES,
    }
    for key, expected in expected_integers.items():
        if integer(row.get(key)) != expected:
            raise CertificationError(f"certifier {key} mismatch for {expected_hash}")
    completed = integer(row.get("completed_frames"))
    if completed is None or completed < 0 or completed > MEASURED_FRAMES:
        raise CertificationError(f"certifier completed_frames invalid for {expected_hash}")
    target_fps = finite_number(row.get("target_fps"))
    frame_budget = finite_number(row.get("frame_budget_ms"))
    if target_fps != REQUIRED_FPS or frame_budget is None or not math.isclose(
        frame_budget, MAX_P95_MS, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise CertificationError(f"certifier timing policy mismatch for {expected_hash}")
    for key in ("performed", "process_passed", "performance_passed"):
        if not isinstance(row.get(key), bool):
            raise CertificationError(f"certifier {key} is not boolean for {expected_hash}")
    if not isinstance(row.get("error"), str):
        raise CertificationError(f"certifier error field is invalid for {expected_hash}")

    timings: dict[str, float] = {}
    for key in (
        "mean_ms",
        "median_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "mean_gpu_ms",
        "p95_gpu_ms",
        "p95_fps",
    ):
        number = finite_number(row.get(key))
        if number is None or number < 0.0:
            raise CertificationError(f"certifier {key} is invalid for {expected_hash}")
        timings[key] = number

    process_passed = row["process_passed"]
    performance_passed = row["performance_passed"]
    if not row["performed"]:
        raise CertificationError(f"certifier did not perform recipe {expected_hash}")
    if process_passed:
        if completed != MEASURED_FRAMES:
            raise CertificationError(f"certifier result is incomplete for {expected_hash}")
        if any(timings[key] <= 0.0 for key in ("mean_ms", "median_ms", "p95_ms", "p99_ms", "max_ms")):
            raise CertificationError(f"certifier wall timing is empty for {expected_hash}")
        if not fps_consistent(timings["p95_ms"], timings["p95_fps"]):
            raise CertificationError(f"certifier p95_fps is inconsistent for {expected_hash}")
    elif performance_passed:
        raise CertificationError(f"performance passed without processing for {expected_hash}")

    expected_performance = (
        process_passed
        and timings["mean_ms"] <= MAX_P95_MS
        and timings["p95_ms"] <= MAX_P95_MS
    )
    if performance_passed != expected_performance:
        raise CertificationError(f"certifier performance flag is inconsistent for {expected_hash}")
    return row


def published_record(target: Target, tool_row: dict[str, Any]) -> dict[str, Any]:
    process_passed = tool_row["process_passed"]
    performance_passed = tool_row["performance_passed"]
    if performance_passed:
        status = "passed"
    elif process_passed:
        status = "failed"
    else:
        status = "error"
    return {
        "candidate_id": target.candidate_id,
        "recipe_hash": target.recipe_hash,
        "recipe_sha256": target.recipe_sha256,
        "canonical_sha256": target.canonical_sha256,
        "cache_key": target.cache_key,
        "status": status,
        "backend": tool_row["backend"],
        "width": tool_row["width"],
        "height": tool_row["height"],
        "warmup_frames": tool_row["warmup_frames"],
        "frames_measured": tool_row["completed_frames"],
        "target_fps": tool_row["target_fps"],
        "frame_budget_ms": tool_row["frame_budget_ms"],
        "mean_ms": tool_row["mean_ms"],
        "median_ms": tool_row["median_ms"],
        "p95_ms": tool_row["p95_ms"],
        "p99_ms": tool_row["p99_ms"],
        "max_ms": tool_row["max_ms"],
        "p95_fps": tool_row["p95_fps"],
        "mean_gpu_ms": tool_row["mean_gpu_ms"],
        "p95_gpu_ms": tool_row["p95_gpu_ms"],
        "performed": tool_row["performed"],
        "process_passed": process_passed,
        "performance_passed": performance_passed,
        "error": tool_row["error"],
    }


def valid_cached_pass(row: Any, target: Target) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("cache_key") != target.cache_key:
        return False
    if row.get("recipe_hash") != target.recipe_hash or row.get("recipe_sha256") != target.recipe_sha256:
        return False
    if row.get("canonical_sha256") != target.canonical_sha256 or row.get("status") != "passed":
        return False
    if row.get("backend") != "metal" or row.get("width") != WIDTH or row.get("height") != HEIGHT:
        return False
    if integer(row.get("warmup_frames")) is None or row["warmup_frames"] < WARMUP_FRAMES:
        return False
    if integer(row.get("frames_measured")) is None or row["frames_measured"] < MEASURED_FRAMES:
        return False
    if row.get("process_passed") is not True or row.get("performance_passed") is not True:
        return False
    mean_ms = finite_number(row.get("mean_ms"))
    p95_ms = finite_number(row.get("p95_ms"))
    p95_fps = finite_number(row.get("p95_fps"))
    if (
        mean_ms is None
        or p95_ms is None
        or p95_fps is None
        or mean_ms <= 0.0
        or p95_ms <= 0.0
        or mean_ms > MAX_P95_MS
        or p95_ms > MAX_P95_MS
        or not fps_consistent(p95_ms, p95_fps)
    ):
        return False
    return True


def load_passing_cache(output_path: Path, targets: list[Target]) -> dict[str, dict[str, Any]]:
    if not output_path.is_file():
        return {}
    try:
        payload = strict_json_loads(output_path.read_bytes(), "existing certification cache")
    except (CertificationError, OSError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return {}
    records = payload.get("records")
    if not isinstance(records, dict):
        return {}
    cached: dict[str, dict[str, Any]] = {}
    for target in targets:
        row = records.get(target.recipe_hash)
        if valid_cached_pass(row, target):
            reused = copy.deepcopy(row)
            reused["candidate_id"] = target.candidate_id
            cached[target.recipe_hash] = reused
    return cached


def run_child(
    command: list[str], description: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    global ACTIVE_PROCESS
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=environment,
        )
    except OSError as error:
        raise CertificationError(f"failed to start {description}: {error}") from error
    ACTIVE_PROCESS = process
    try:
        stdout, stderr = process.communicate()
    finally:
        ACTIVE_PROCESS = None
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise CertificationError(
            f"{description} failed with exit {completed.returncode}: {detail[-4000:]}"
        )
    return completed


def read_tool_results(path: Path, expected_hashes: set[str]) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CertificationError(f"cannot read certifier output: {error}") from error
    if raw and not raw.endswith(b"\n"):
        raise CertificationError("certifier output has an incomplete final row")
    results: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise CertificationError(f"certifier output has a blank row {line_number}")
        row = strict_json_loads(line, f"certifier row {line_number}")
        if not isinstance(row, dict):
            raise CertificationError(f"certifier row {line_number} is not an object")
        recipe_hash = row.get("recipe_hash")
        if recipe_hash not in expected_hashes:
            raise CertificationError(f"certifier returned unexpected recipe {recipe_hash}")
        if recipe_hash in results:
            raise CertificationError(f"certifier returned duplicate recipe {recipe_hash}")
        results[recipe_hash] = validate_tool_row(row, recipe_hash)
    missing = sorted(expected_hashes - set(results))
    if missing:
        raise CertificationError(f"certifier omitted recipes: {', '.join(missing[:3])}")
    return results


def ensure_paths_are_safe(output: Path, protected: Iterable[Path]) -> None:
    output_resolved = output.expanduser().resolve()
    for path in protected:
        if output_resolved == path.expanduser().resolve():
            raise CertificationError(f"output would overwrite required input: {path}")


def resolve_metallib(binary_path: Path, requested: Path | None) -> Path:
    candidates: list[Path] = []
    if requested is not None:
        candidates.append(requested.expanduser())
    else:
        environment_path = os.environ.get("GLIC_METALLIB_PATH")
        if environment_path:
            candidates.append(Path(environment_path).expanduser())
        candidates.extend(
            (
                binary_path.parent / "glic_realtime.metallib",
                binary_path.parent.parent / "lib" / "glic" / "glic_realtime.metallib",
            )
        )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise CertificationError(
        "glic_realtime.metallib was not found; place it beside the certifier or pass --metallib"
    )


def hardware_identity() -> dict[str, str]:
    hostname = socket.gethostname().strip()
    # ``platform.platform()`` includes interpreter-binary details on macOS
    # (for example a trailing ``Mach-O`` with Homebrew Python but not the
    # system Python). Those details are unrelated to the benchmark machine and
    # caused cache misses when the pipeline and a manual invocation used
    # different Python installations. Kernel values are stable across them.
    system_name = platform_module.system().strip()
    kernel_release = platform_module.release().strip()
    machine = platform_module.machine().strip()
    platform_name = "-".join((system_name, kernel_release, machine))
    hw_model = ""
    for command in (
        ["/usr/sbin/sysctl", "-n", "hw.model"],
        ["/sbin/sysctl", "-n", "hw.model"],
    ):
        if not Path(command[0]).is_file():
            continue
        try:
            completed = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        hw_model = completed.stdout.strip()
        if hw_model:
            break
    if not hw_model:
        # Keep the cache machine-specific on non-macOS test hosts while making
        # the fallback explicit instead of claiming a verified Apple hw.model.
        hw_model = f"unavailable:{machine}"
    if not hostname or not platform_name:
        raise CertificationError("hardware identity is incomplete")
    identity = {
        "hostname": hostname,
        "platform": platform_name,
        "hw_model": hw_model,
    }
    identity["fingerprint"] = sha256_bytes(canonical_json_bytes(identity))
    return identity


def resolve_default_input(
    archive: Any, run_dir: Path, script_repo_root: Path
) -> Path:
    if not isinstance(archive, dict):
        raise CertificationError("ranking archive root must be an object")
    inputs = archive.get("inputs")
    if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], str):
        raise CertificationError("ranking archive has no default input path")
    raw_path = Path(inputs[0]).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    run_repo_root = run_dir.parent.parent if run_dir.parent.name == "search-runs" else script_repo_root
    candidates = [run_repo_root / raw_path]
    if run_repo_root != script_repo_root:
        candidates.append(script_repo_root / raw_path)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    return candidates[0].resolve()


def parse_args() -> argparse.Namespace:
    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="?")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument(
        "--input",
        type=Path,
        help="PNG to certify; defaults to archive inputs[0] resolved against the repository root",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path(os.environ.get("GLIC_REALTIME_CERTIFIER", repo_root / "build" / "glic_realtime_certify")),
    )
    parser.add_argument(
        "--metallib",
        type=Path,
        help="Metal library to hash and force-load; defaults beside the certifier",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def make_boundary_tool_row(recipe_hash: str, p95_ms: float, passed: bool) -> dict[str, Any]:
    return {
        "schema": TOOL_RESULT_SCHEMA,
        "recipe_hash": recipe_hash,
        "backend": "metal",
        "width": WIDTH,
        "height": HEIGHT,
        "target_fps": REQUIRED_FPS,
        "frame_budget_ms": MAX_P95_MS,
        "warmup_frames": WARMUP_FRAMES,
        "measured_frames": MEASURED_FRAMES,
        "completed_frames": MEASURED_FRAMES,
        "mean_ms": MAX_P95_MS,
        "median_ms": 1.0,
        "p95_ms": p95_ms,
        "p99_ms": p95_ms,
        "max_ms": p95_ms,
        "mean_gpu_ms": 0.5,
        "p95_gpu_ms": 0.75,
        "p95_fps": 1000.0 / p95_ms,
        "performed": True,
        "process_passed": True,
        "performance_passed": passed,
        "error": "" if passed else "below_30_fps_at_960x540",
    }


def run_selftest() -> int:
    recipe_a = {"strength": 1.0, "channels": [{"quantization": 20}], "color_space": 1}
    recipe_b = {"color_space": 1, "channels": [{"quantization": 20}], "strength": 1.0}
    if recipe_sha256(recipe_a) != recipe_sha256(recipe_b):
        raise AssertionError("canonical recipe JSON is order-dependent")
    sample_hash = "0123456789abcdef"
    validate_tool_row(make_boundary_tool_row(sample_hash, MAX_P95_MS, True), sample_hash)
    invalid = make_boundary_tool_row(sample_hash, math.nextafter(MAX_P95_MS, math.inf), True)
    try:
        validate_tool_row(invalid, sample_hash)
    except CertificationError:
        pass
    else:
        raise AssertionError("over-budget result was accepted")
    target = Target(
        candidate_id=1,
        recipe_hash=sample_hash,
        recipe=recipe_a,
        recipe_sha256=recipe_sha256(recipe_a),
        canonical="v1|sample",
        canonical_sha256="canonical-sha",
    )
    first = make_cache_key(target, "binary", "metallib", "input", "hardware")
    second = make_cache_key(
        target, "changed-binary", "metallib", "input", "hardware"
    )
    if first == second:
        raise AssertionError("cache key ignores binary identity")
    changed_shader = make_cache_key(
        target, "binary", "changed-metallib", "input", "hardware"
    )
    if first == changed_shader:
        raise AssertionError("cache key ignores Metal library identity")
    duplicate_json = '{"a":1,"a":2}'
    try:
        strict_json_loads(duplicate_json, "selftest duplicate")
    except CertificationError:
        pass
    else:
        raise AssertionError("duplicate JSON keys were accepted")
    print("SELFTEST PASSED")
    return 0


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, handle_process_signal)
    signal.signal(signal.SIGTERM, handle_process_signal)
    if args.selftest:
        try:
            return run_selftest()
        except (AssertionError, CertificationError) as error:
            print(f"SELFTEST FAILED: {error}", file=sys.stderr)
            return 1
    if args.run_dir is None:
        print("run_dir is required", file=sys.stderr)
        return 2

    run_dir = args.run_dir.expanduser().resolve()
    archive_path = (args.archive or run_dir / "ranking-archive.json").expanduser().resolve()
    candidates_path = (args.candidates or run_dir / "candidates.ndjson").expanduser().resolve()
    binary_path = args.binary.expanduser().resolve()
    output_path = (args.output or run_dir / "performance-certifications.json").expanduser().resolve()

    try:
        for description, path in (
            ("run directory", run_dir),
            ("ranking archive", archive_path),
            ("candidates log", candidates_path),
            ("certifier binary", binary_path),
        ):
            if description == "run directory":
                if not path.is_dir():
                    raise CertificationError(f"{description} not found: {path}")
            elif not path.is_file():
                raise CertificationError(f"{description} not found: {path}")
        if not os.access(binary_path, os.X_OK):
            raise CertificationError(f"certifier binary is not executable: {binary_path}")

        archive_bytes = archive_path.read_bytes()
        archive_sha256 = sha256_bytes(archive_bytes)
        archive = strict_json_loads(archive_bytes, "ranking archive")
        input_path = (
            args.input.expanduser().resolve()
            if args.input is not None
            else resolve_default_input(
                archive, run_dir, Path(__file__).resolve().parent.parent
            )
        )
        if not input_path.is_file():
            raise CertificationError(f"input PNG not found: {input_path}")
        if input_path.suffix.lower() != ".png":
            raise CertificationError(f"certification input must be PNG: {input_path}")
        metallib_path = resolve_metallib(binary_path, args.metallib)
        ensure_paths_are_safe(
            output_path,
            (archive_path, candidates_path, input_path, binary_path, metallib_path),
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        input_sha256 = sha256_file(input_path)
        binary_sha256 = sha256_file(binary_path)
        metallib_sha256 = sha256_file(metallib_path)
        hardware = hardware_identity()
        targets = extract_targets(archive)
        targets = resolve_canonicals(candidates_path, targets)
        targets = with_cache_keys(
            targets,
            binary_sha256,
            metallib_sha256,
            input_sha256,
            hardware["fingerprint"],
        )

        certifier_environment = os.environ.copy()
        certifier_environment["GLIC_METALLIB_PATH"] = str(metallib_path)
        selftest = run_child(
            [str(binary_path), "--selftest"],
            "certifier selftest",
            certifier_environment,
        )
        if "SELFTEST PASSED" not in selftest.stdout:
            raise CertificationError("certifier selftest did not report success")

        reused_records = {} if args.force else load_passing_cache(output_path, targets)
        pending = [target for target in targets if target.recipe_hash not in reused_records]
        records = dict(reused_records)

        with tempfile.TemporaryDirectory(
            prefix=".glic-performance-certification-", dir=output_path.parent
        ) as temporary_name:
            temporary_dir = Path(temporary_name)
            if pending:
                recipes_path = temporary_dir / "recipes.tsv"
                results_path = temporary_dir / "results.ndjson"
                recipes_text = "".join(
                    f"{target.recipe_hash}\t{target.canonical}\n" for target in pending
                )
                recipes_path.write_text(recipes_text, encoding="utf-8")
                run_child(
                    [
                        str(binary_path),
                        "--input",
                        str(input_path),
                        "--recipes",
                        str(recipes_path),
                        "--output",
                        str(results_path),
                    ],
                    "Metal performance certifier",
                    certifier_environment,
                )
                tool_results = read_tool_results(
                    results_path, {target.recipe_hash for target in pending}
                )
                for target in pending:
                    records[target.recipe_hash] = published_record(
                        target, tool_results[target.recipe_hash]
                    )

        expected_hashes = {target.recipe_hash for target in targets}
        if set(records) != expected_hashes:
            raise CertificationError("certification record set is incomplete")
        if sha256_file(archive_path) != archive_sha256:
            raise CertificationError("ranking archive changed during certification")
        if sha256_file(input_path) != input_sha256:
            raise CertificationError("certification input changed during certification")
        if sha256_file(binary_path) != binary_sha256:
            raise CertificationError("certifier binary changed during certification")
        if sha256_file(metallib_path) != metallib_sha256:
            raise CertificationError("Metal library changed during certification")

        ordered_records = {recipe_hash: records[recipe_hash] for recipe_hash in sorted(records)}
        status_counts = {"passed": 0, "failed": 0, "error": 0}
        for row in ordered_records.values():
            status = row.get("status")
            if status not in status_counts:
                raise CertificationError(f"invalid published status: {status}")
            status_counts[status] += 1
        payload = {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": {
                "run_dir": str(run_dir),
                "archive_path": str(archive_path),
                "archive_sha256": archive_sha256,
                "candidates_path": str(candidates_path),
                "input_path": str(input_path),
                "input_sha256": input_sha256,
                "binary_path": str(binary_path),
                "binary_sha256": binary_sha256,
                "metallib_path": str(metallib_path),
                "metallib_sha256": metallib_sha256,
                "hardware": hardware,
            },
            "policy": POLICY,
            "stats": {
                "expected": len(targets),
                "certified": len(ordered_records),
                "passed": status_counts["passed"],
                "failed": status_counts["failed"],
                "error": status_counts["error"],
                "reused": len(reused_records),
                "new": len(pending),
            },
            "records": ordered_records,
        }
        atomic_write_text(
            output_path,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
        )
    except (CertificationError, OSError) as error:
        print(
            f"performance certification failed closed; previous result was preserved: {error}",
            file=sys.stderr,
        )
        return 1

    print(
        f"certified {len(records)} archive recipes at {WIDTH}x{HEIGHT} Metal; "
        f"passed={status_counts['passed']} failed={status_counts['failed']} "
        f"error={status_counts['error']} reused={len(reused_records)} output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
