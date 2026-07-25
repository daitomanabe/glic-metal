#!/usr/bin/env python3
"""Mutate x265 4.2 analysis-save motion vectors before HEVC CABAC coding.

This module intentionally supports one narrow, reproducible file contract:
x265 analysis reuse level 10, YUV input, CU-tree disabled, CTU distortion
disabled, and B-frame intra prediction disabled.  The matching encode command
is built by process_native_syntax_glitch.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct


HEADER_INT_COUNT = 20
HEADER_BYTES = HEADER_INT_COUNT * 4
FRAME_HEADER_BYTES = 36
WEIGHT_PARAM_BYTES = 16
MV_BYTES = 8
IMPLEMENTATION_LEVEL = (
    "native_hevc_x265_analysis_motion_vector_injection_before_cabac"
)

X265_TYPE_IDR = 1
X265_TYPE_I = 2
X265_TYPE_P = 3
X265_TYPE_BREF = 4
X265_TYPE_B = 5
INTRA_TYPES = {X265_TYPE_IDR, X265_TYPE_I}
INTER_TYPES = {X265_TYPE_P, X265_TYPE_BREF, X265_TYPE_B}


class X265AnalysisError(ValueError):
    """Raised when an analysis file is outside the supported x265 contract."""


@dataclass(frozen=True)
class AnalysisHeader:
    analysis_reuse_level: int
    cutree: int
    width: int
    height: int
    max_cu_size: int
    ctu_distortion_refine: int
    bframes: int


@dataclass(frozen=True)
class MotionPlane:
    frame_index: int
    poc: int
    slice_type: int
    direction: int
    depth_bytes: int
    ref_offset: int
    mv_offset: int


def _mix64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return value ^ (value >> 31)


def _selected(amount: float, seed: int, *coordinates: int) -> bool:
    if amount <= 0.0:
        return False
    if amount >= 1.0:
        return True
    value = seed & 0xFFFFFFFFFFFFFFFF
    for coordinate in coordinates:
        value = _mix64(value ^ (coordinate + 0x9E3779B97F4A7C15))
    return value < int(amount * (1 << 64))


def _parse_header(data: bytes | bytearray) -> AnalysisHeader:
    if len(data) < HEADER_BYTES:
        raise X265AnalysisError("x265 analysis file is shorter than its header")
    values = struct.unpack_from("<20i", data, 0)
    header = AnalysisHeader(
        analysis_reuse_level=values[15],
        cutree=values[16],
        width=values[17],
        height=values[18],
        max_cu_size=values[19],
        ctu_distortion_refine=values[13],
        bframes=values[7],
    )
    if header.analysis_reuse_level != 10:
        raise X265AnalysisError(
            "x265 analysis-save-reuse-level must be exactly 10"
        )
    if header.cutree != 0:
        raise X265AnalysisError("x265 analysis file must use --no-cutree")
    if header.ctu_distortion_refine != 0:
        raise X265AnalysisError(
            "x265 analysis file must disable CTU distortion refinement"
        )
    if (
        header.width < 2
        or header.height < 2
        or header.max_cu_size not in (16, 32, 64)
    ):
        raise X265AnalysisError("x265 analysis header dimensions are invalid")
    return header


def parse_motion_planes(
    data: bytes | bytearray, *, b_intra: bool = False
) -> tuple[AnalysisHeader, list[MotionPlane], int]:
    """Return validated MV/ref array locations and total frame count."""
    header = _parse_header(data)
    planes: list[MotionPlane] = []
    offset = HEADER_BYTES
    frame_index = 0
    while offset < len(data):
        if offset + FRAME_HEADER_BYTES > len(data):
            raise X265AnalysisError("truncated x265 analysis frame header")
        (
            frame_size,
            depth_bytes,
            poc,
            slice_type,
            _scene_cut,
            _satd,
            num_cus,
            num_partitions,
        ) = struct.unpack_from("<IIiiiqII", data, offset)
        if frame_size < FRAME_HEADER_BYTES or offset + frame_size > len(data):
            raise X265AnalysisError("invalid x265 analysis frame size")
        if depth_bytes < 1 or num_cus < 1 or num_partitions < 1:
            raise X265AnalysisError("invalid x265 analysis frame dimensions")
        if slice_type not in INTRA_TYPES | INTER_TYPES:
            raise X265AnalysisError(
                f"unsupported x265 slice type {slice_type}"
            )

        if slice_type in INTRA_TYPES:
            expected = (
                FRAME_HEADER_BYTES
                + depth_bytes * 3
                + num_cus * num_partitions
            )
        else:
            directions = 1 if slice_type == X265_TYPE_P else 2
            intra_in_inter = slice_type == X265_TYPE_P or b_intra
            cursor = (
                offset
                + FRAME_HEADER_BYTES
                + WEIGHT_PARAM_BYTES * 3 * directions
            )
            # depth, modes, partition size, merge flag, inter direction
            cursor += depth_bytes * 5
            if intra_in_inter:
                cursor += depth_bytes  # chroma intra modes
            for direction in range(directions):
                cursor += depth_bytes  # MVP index
                ref_offset = cursor
                cursor += depth_bytes
                mv_offset = cursor
                cursor += depth_bytes * MV_BYTES
                planes.append(
                    MotionPlane(
                        frame_index=frame_index,
                        poc=poc,
                        slice_type=slice_type,
                        direction=direction,
                        depth_bytes=depth_bytes,
                        ref_offset=ref_offset,
                        mv_offset=mv_offset,
                    )
                )
            if intra_in_inter:
                cursor += num_cus * num_partitions
            expected = cursor - offset
        if expected != frame_size:
            raise X265AnalysisError(
                "x265 analysis record layout does not match the supported "
                f"4.2 contract at POC {poc}: expected {expected}, "
                f"found {frame_size}"
            )
        offset += frame_size
        frame_index += 1
    if offset != len(data):
        raise X265AnalysisError("x265 analysis file has trailing bytes")
    if not planes:
        raise X265AnalysisError("x265 analysis file contains no inter-frame MVs")
    return header, planes, frame_index


def _clamp_mv(value: int) -> int:
    # Keep injected QPEL vectors safely inside x265's practical search range.
    return max(-8192, min(8192, value))


def _transform(
    effect: str,
    x: int,
    y: int,
    *,
    index: int,
    count: int,
    width: int,
    height: int,
    amount: float,
    previous: tuple[int, int] | None,
) -> tuple[int, int]:
    if effect == "compressed_motion_vector_mirror":
        return _clamp_mv(-x), y
    if effect == "compressed_motion_vector_quantizer":
        step = max(2, round(2 + amount * 30))
        return (
            _clamp_mv(int(round(x / step) * step)),
            _clamp_mv(int(round(y / step) * step)),
        )
    if effect == "compressed_motion_vector_freeze" and previous is not None:
        return previous
    if effect == "compressed_motion_vector_vortex":
        aspect = width / max(height, 1)
        columns = max(1, round(math.sqrt(max(count, 1) * aspect)))
        rows = max(1, math.ceil(count / columns))
        column = index % columns
        row = index // columns
        center_x = max(columns - 1, 1) * 0.5
        center_y = max(rows - 1, 1) * 0.5
        dx = (column - center_x) / max(center_x, 1.0)
        dy = (row - center_y) / max(center_y, 1.0)
        strength = 4.0 + amount * 44.0
        return (
            _clamp_mv(round(x - dy * strength)),
            _clamp_mv(round(y + dx * strength)),
        )
    return x, y


def mutate_analysis_bytes(
    data: bytes,
    effect: str,
    amount: float,
    seed: int,
) -> tuple[bytes, dict]:
    """Mutate valid referenced x265 MVs and return bytes plus evidence."""
    supported = {
        "compressed_motion_vector_vortex",
        "compressed_motion_vector_mirror",
        "compressed_motion_vector_quantizer",
        "compressed_motion_vector_freeze",
    }
    if effect not in supported:
        raise X265AnalysisError(f"unsupported HEVC MV effect: {effect}")
    if not 0.0 <= amount <= 1.0:
        raise X265AnalysisError("amount must be between 0 and 1")

    mutable = bytearray(data)
    header, planes, frame_count = parse_motion_planes(mutable)
    total = 0
    selected = 0
    changed_values = 0
    frames_with_changes: set[int] = set()
    previous_by_direction: dict[int, list[tuple[int, int] | None]] = {}

    for plane in planes:
        prior = previous_by_direction.get(plane.direction, [])
        current: list[tuple[int, int] | None] = [None] * plane.depth_bytes
        for index in range(plane.depth_bytes):
            ref_index = struct.unpack_from(
                "<b", mutable, plane.ref_offset + index
            )[0]
            if ref_index < 0:
                continue
            position = plane.mv_offset + index * MV_BYTES
            before = struct.unpack_from("<ii", mutable, position)
            current[index] = before
            total += 1
            if not _selected(
                amount,
                seed,
                plane.frame_index,
                plane.direction,
                index,
            ):
                continue
            selected += 1
            previous = prior[index] if index < len(prior) else None
            after = _transform(
                effect,
                before[0],
                before[1],
                index=index,
                count=plane.depth_bytes,
                width=header.width,
                height=header.height,
                amount=amount,
                previous=previous,
            )
            if after != before:
                struct.pack_into("<ii", mutable, position, *after)
                current[index] = after
                changed_values += sum(
                    left != right for left, right in zip(before, after)
                )
                frames_with_changes.add(plane.frame_index)
        previous_by_direction[plane.direction] = current

    evidence = {
        "feature": "x265_analysis_mv",
        "effect": effect,
        "seed": seed,
        "amount": amount,
        "implementation_level": IMPLEMENTATION_LEVEL,
        "x265_analysis_reuse_level": header.analysis_reuse_level,
        "analysis_width": header.width,
        "analysis_height": header.height,
        "frame_count": frame_count,
        "motion_planes": len(planes),
        "total_vector_candidates": total,
        "selected_vector_candidates": selected,
        "changed_values": changed_values,
        "frames_with_changes": len(frames_with_changes),
        "valid_reference_vectors_only": True,
        "mv_units": "quarter_pixel",
        "mv_clamp": [-8192, 8192],
    }
    return bytes(mutable), evidence


def mutate_analysis_file(
    source: Path,
    destination: Path,
    effect: str,
    amount: float,
    seed: int,
) -> dict:
    source_bytes = source.read_bytes()
    mutated, evidence = mutate_analysis_bytes(
        source_bytes, effect, amount, seed
    )
    destination.write_bytes(mutated)
    evidence["source_analysis_sha256"] = hashlib.sha256(
        source_bytes
    ).hexdigest()
    evidence["mutated_analysis_sha256"] = hashlib.sha256(mutated).hexdigest()
    return evidence
