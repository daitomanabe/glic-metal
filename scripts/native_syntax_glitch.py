#!/usr/bin/env python3
"""Pure helpers for FFglitch MPEG-2 compressed-syntax mutations."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Callable


MOTION_EFFECTS = (
    "compressed_motion_vector_vortex",
    "compressed_motion_vector_mirror",
    "compressed_motion_vector_quantizer",
    "compressed_motion_vector_freeze",
)
COEFFICIENT_EFFECTS = (
    "compressed_coefficient_sign_flip",
    "compressed_coefficient_band_gate",
    "compressed_coefficient_transplant",
    "compressed_coefficient_scan_fold",
)
QUANTIZER_EFFECTS = (
    "compressed_quantizer_checkerboard",
    "compressed_quantizer_wave",
    "compressed_quantizer_raster",
    "compressed_quantizer_pulse",
)
EFFECTS = MOTION_EFFECTS + COEFFICIENT_EFFECTS + QUANTIZER_EFFECTS
FEATURE_FOR_EFFECT = {
    **{effect: "mv" for effect in MOTION_EFFECTS},
    **{effect: "q_dct" for effect in COEFFICIENT_EFFECTS},
    **{effect: "qscale" for effect in QUANTIZER_EFFECTS},
}
IMPLEMENTATION_LEVEL = {
    effect: (
        "native_mpeg2_ffglitch_motion_vector_entropy_transplication"
        if effect in MOTION_EFFECTS
        else (
            "native_mpeg2_ffglitch_quantized_dct_entropy_transplication"
            if effect in COEFFICIENT_EFFECTS
            else "native_mpeg2_ffglitch_quantizer_scale_entropy_transplication"
        )
    )
    for effect in EFFECTS
}
STREAM_CODECS_BY_FEATURE = {
    "mv": {"mpeg2video", "mpeg4"},
    "q_dct": {"mpeg2video"},
    "qscale": {"mpeg2video"},
}


class SyntaxMutationError(ValueError):
    """Raised when exported syntax data does not match the supported contract."""


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


def _is_vector(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(component, (int, float))
            and not isinstance(component, bool)
            and math.isfinite(float(component))
            for component in value
        )
    )


def _visit_vectors(
    value: object,
    callback: Callable[[list, tuple[int, ...]], None],
    path: tuple[int, ...] = (),
) -> None:
    if _is_vector(value):
        callback(value, path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _visit_vectors(child, callback, path + (index,))


def _vector_at_path(value: object, path: tuple[int, ...]) -> list | None:
    current = value
    for index in path:
        if not isinstance(current, list) or index >= len(current):
            return None
        current = current[index]
    return current if _is_vector(current) else None


def _validate_document(document: object, feature: str) -> dict:
    if not isinstance(document, dict):
        raise SyntaxMutationError("FFglitch export must be a JSON object")
    if document.get("ffedit_version") is None:
        raise SyntaxMutationError("FFglitch export is missing ffedit_version")
    features = document.get("features")
    if not isinstance(features, list) or feature not in features:
        raise SyntaxMutationError(
            f"FFglitch export does not contain requested feature {feature}"
        )
    streams = document.get("streams")
    if not isinstance(streams, list) or not streams:
        raise SyntaxMutationError("FFglitch export has no streams")
    supported_codecs = STREAM_CODECS_BY_FEATURE[feature]
    for stream in streams:
        if (
            not isinstance(stream, dict)
            or stream.get("codec") not in supported_codecs
        ):
            raise SyntaxMutationError(
                f"direct {feature} mutation supports stream codecs: "
                f"{', '.join(sorted(supported_codecs))}"
            )
        if not isinstance(stream.get("frames"), list):
            raise SyntaxMutationError("FFglitch stream has no frame list")
    return document


def _motion_transform(
    effect: str,
    vector: list,
    *,
    row: int,
    column: int,
    rows: int,
    columns: int,
    amount: float,
    previous: list | None,
) -> tuple[int, int]:
    horizontal = int(round(float(vector[0])))
    vertical = int(round(float(vector[1])))
    if effect == "compressed_motion_vector_mirror":
        return -horizontal, vertical
    if effect == "compressed_motion_vector_quantizer":
        step = max(2, round(2 + amount * 30))
        return (
            int(round(horizontal / step) * step),
            int(round(vertical / step) * step),
        )
    if effect == "compressed_motion_vector_freeze" and previous is not None:
        return int(previous[0]), int(previous[1])
    if effect == "compressed_motion_vector_vortex":
        center_x = max(columns - 1, 1) * 0.5
        center_y = max(rows - 1, 1) * 0.5
        dx = (column - center_x) / max(center_x, 1.0)
        dy = (row - center_y) / max(center_y, 1.0)
        strength = 4.0 + amount * 44.0
        return (
            int(round(horizontal - dy * strength)),
            int(round(vertical + dx * strength)),
        )
    return horizontal, vertical


def _mutate_motion(document: dict, effect: str, amount: float, seed: int) -> dict:
    total = 0
    selected = 0
    changed_values = 0
    frames_with_changes: set[int] = set()
    previous_fields: dict[tuple[int, str], object] = {}

    for stream_index, stream in enumerate(document["streams"]):
        for frame_index, frame in enumerate(stream["frames"]):
            motion = frame.get("mv")
            if not isinstance(motion, dict):
                continue
            motion["overflow"] = "truncate"
            for direction_index, direction in enumerate(("forward", "backward")):
                field = motion.get(direction)
                if not isinstance(field, list):
                    continue
                previous_field = previous_fields.get((stream_index, direction))
                rows = len(field)
                columns = max(
                    (len(row) for row in field if isinstance(row, list)),
                    default=0,
                )
                for row_index, row in enumerate(field):
                    if not isinstance(row, list):
                        continue
                    for column_index, entry in enumerate(row):
                        def mutate(vector: list, subpath: tuple[int, ...]) -> None:
                            nonlocal total, selected, changed_values
                            candidate_index = total
                            total += 1
                            if not _selected(
                                amount,
                                seed,
                                stream_index,
                                frame_index,
                                direction_index,
                                row_index,
                                column_index,
                                *subpath,
                            ):
                                return
                            selected += 1
                            previous = None
                            if isinstance(previous_field, list):
                                previous_entry = (
                                    previous_field[row_index][column_index]
                                    if row_index < len(previous_field)
                                    and isinstance(previous_field[row_index], list)
                                    and column_index
                                    < len(previous_field[row_index])
                                    else None
                                )
                                previous = _vector_at_path(
                                    previous_entry, subpath
                                )
                            before = (int(vector[0]), int(vector[1]))
                            after = _motion_transform(
                                effect,
                                vector,
                                row=row_index,
                                column=column_index,
                                rows=rows,
                                columns=columns,
                                amount=amount,
                                previous=previous,
                            )
                            vector[0], vector[1] = after
                            differences = sum(
                                left != right
                                for left, right in zip(before, after)
                            )
                            if differences:
                                changed_values += differences
                                frames_with_changes.add(frame_index)

                        _visit_vectors(entry, mutate)
                previous_fields[(stream_index, direction)] = copy.deepcopy(field)

    return {
        "feature": "mv",
        "total_vector_candidates": total,
        "selected_vector_candidates": selected,
        "changed_values": changed_values,
        "frames_with_changes": len(frames_with_changes),
        "overflow_policy": "truncate",
    }


def _is_coefficient_block(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 64
        and all(
            isinstance(component, int) and not isinstance(component, bool)
            for component in value
        )
    )


def _mutate_coefficients(
    document: dict, effect: str, amount: float, seed: int
) -> dict:
    blocks = 0
    selected_blocks = 0
    candidate_values = 0
    changed_values = 0
    frames_with_changes: set[int] = set()
    previous_blocks: dict[tuple[int, int, int, int], list] = {}

    for stream_index, stream in enumerate(document["streams"]):
        for frame_index, frame in enumerate(stream["frames"]):
            q_dct = frame.get("q_dct")
            data = q_dct.get("data") if isinstance(q_dct, dict) else None
            if not isinstance(data, list):
                continue
            next_previous: dict[tuple[int, int, int, int], list] = {}
            for row_index, row in enumerate(data):
                if not isinstance(row, list):
                    continue
                for column_index, macroblock in enumerate(row):
                    if not isinstance(macroblock, list):
                        continue
                    for block_index, block in enumerate(macroblock):
                        if not _is_coefficient_block(block):
                            continue
                        key = (
                            stream_index,
                            row_index,
                            column_index,
                            block_index,
                        )
                        previous = previous_blocks.get(key)
                        next_previous[key] = block.copy()
                        block_number = blocks
                        blocks += 1
                        block_selected = _selected(
                            amount,
                            seed,
                            stream_index,
                            frame_index,
                            row_index,
                            column_index,
                            block_index,
                        )
                        if not block_selected:
                            continue
                        selected_blocks += 1
                        before = block.copy()
                        if effect == "compressed_coefficient_sign_flip":
                            for coefficient_index in range(1, 64):
                                if block[coefficient_index] != 0:
                                    candidate_values += 1
                                    if _selected(
                                        max(0.2, amount),
                                        seed ^ 0x5349474E,
                                        block_number,
                                        coefficient_index,
                                    ):
                                        block[coefficient_index] *= -1
                        elif effect == "compressed_coefficient_band_gate":
                            cutoff = max(2, round(30 - amount * 24))
                            for coefficient_index in range(cutoff, 64):
                                if block[coefficient_index] != 0:
                                    candidate_values += 1
                                    block[coefficient_index] = 0
                        elif (
                            effect == "compressed_coefficient_transplant"
                            and previous is not None
                        ):
                            candidate_values += 63
                            block[1:] = previous[1:]
                        elif effect == "compressed_coefficient_scan_fold":
                            candidate_values += 63
                            block[1:] = list(reversed(block[1:]))
                        differences = sum(
                            left != right
                            for left, right in zip(before, block)
                        )
                        if differences:
                            changed_values += differences
                            frames_with_changes.add(frame_index)
            previous_blocks = next_previous

    return {
        "feature": "q_dct",
        "total_blocks": blocks,
        "selected_blocks": selected_blocks,
        "candidate_values": candidate_values,
        "changed_values": changed_values,
        "frames_with_changes": len(frames_with_changes),
        "dc_coefficients_preserved": True,
    }


def _qscale_value(
    effect: str,
    *,
    frame_index: int,
    slice_index: int,
    slice_count: int,
    macroblock_offset: int,
    amount: float,
    seed: int,
) -> int:
    low = max(1, round(7 - amount * 5))
    high = min(31, round(10 + amount * 21))
    if effect == "compressed_quantizer_checkerboard":
        return high if (frame_index + slice_index + macroblock_offset) & 1 else low
    if effect == "compressed_quantizer_wave":
        phase = frame_index * 0.61 + slice_index * 0.83
        normalized = 0.5 + 0.5 * math.sin(phase)
        return max(1, min(31, round(low + normalized * (high - low))))
    if effect == "compressed_quantizer_raster":
        denominator = max(slice_count - 1, 1)
        normalized = (
            slice_index / denominator + frame_index * (0.04 + amount * 0.08)
        ) % 1.0
        return max(1, min(31, round(low + normalized * (high - low))))
    if effect == "compressed_quantizer_pulse":
        period = max(2, round(10 - amount * 7))
        pulse = (frame_index + slice_index // 2) % period == 0
        jitter = _mix64(seed ^ frame_index ^ (slice_index << 16)) & 3
        return max(1, min(31, (high if pulse else low) - int(jitter)))
    raise SyntaxMutationError(f"unknown qscale effect: {effect}")


def _mutate_qscale(
    document: dict, effect: str, amount: float, seed: int
) -> dict:
    total = 0
    selected = 0
    changed_values = 0
    frames_with_changes: set[int] = set()
    minimum = 31
    maximum = 1

    for stream_index, stream in enumerate(document["streams"]):
        for frame_index, frame in enumerate(stream["frames"]):
            qscale = frame.get("qscale")
            slices = qscale.get("slice") if isinstance(qscale, dict) else None
            if not isinstance(slices, list):
                continue
            for slice_index, values in enumerate(slices):
                if not isinstance(values, dict):
                    continue
                for value_index, key in enumerate(sorted(values)):
                    value = values[key]
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                    ):
                        raise SyntaxMutationError(
                            "qscale values must be integers"
                        )
                    total += 1
                    if not _selected(
                        amount,
                        seed,
                        stream_index,
                        frame_index,
                        slice_index,
                        value_index,
                    ):
                        minimum = min(minimum, value)
                        maximum = max(maximum, value)
                        continue
                    selected += 1
                    try:
                        macroblock_offset = int(key)
                    except ValueError:
                        macroblock_offset = value_index
                    after = _qscale_value(
                        effect,
                        frame_index=frame_index,
                        slice_index=slice_index,
                        slice_count=len(slices),
                        macroblock_offset=macroblock_offset,
                        amount=amount,
                        seed=seed,
                    )
                    values[key] = after
                    minimum = min(minimum, after)
                    maximum = max(maximum, after)
                    if after != value:
                        changed_values += 1
                        frames_with_changes.add(frame_index)

    return {
        "feature": "qscale",
        "total_quantizer_values": total,
        "selected_quantizer_values": selected,
        "changed_values": changed_values,
        "frames_with_changes": len(frames_with_changes),
        "minimum_quantizer_scale": minimum if total else None,
        "maximum_quantizer_scale": maximum if total else None,
        "legal_quantizer_range": [1, 31],
    }


def implementation_level(effect: str, stream_codec: str = "mpeg2video") -> str:
    if effect not in EFFECTS:
        raise SyntaxMutationError(f"unknown effect: {effect}")
    if effect in MOTION_EFFECTS and stream_codec == "mpeg4":
        return "native_mpeg4_part2_ffglitch_motion_vector_entropy_transplication"
    return IMPLEMENTATION_LEVEL[effect]


def mutate_document(
    document: object, effect: str, amount: float, seed: int
) -> tuple[dict, dict]:
    """Mutate one FFglitch export and return the document plus evidence."""
    if effect not in EFFECTS:
        raise SyntaxMutationError(f"unknown effect: {effect}")
    if not 0.0 <= amount <= 1.0:
        raise SyntaxMutationError("amount must be between 0 and 1")
    feature = FEATURE_FOR_EFFECT[effect]
    validated = _validate_document(document, feature)
    evidence = (
        _mutate_motion(validated, effect, amount, seed)
        if feature == "mv"
        else (
            _mutate_coefficients(validated, effect, amount, seed)
            if feature == "q_dct"
            else _mutate_qscale(validated, effect, amount, seed)
        )
    )
    stream_codec = validated["streams"][0]["codec"]
    evidence.update(
        {
            "effect": effect,
            "seed": seed,
            "amount": amount,
            "implementation_level": implementation_level(
                effect, stream_codec
            ),
            "stream_codec": stream_codec,
        }
    )
    return validated, evidence


def mutate_json_file(
    source: Path,
    destination: Path,
    effect: str,
    amount: float,
    seed: int,
) -> dict:
    document = json.loads(source.read_text(encoding="utf-8"))
    mutated, evidence = mutate_document(document, effect, amount, seed)
    destination.write_text(
        json.dumps(mutated, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    evidence["source_json_sha256"] = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    evidence["mutated_json_sha256"] = hashlib.sha256(
        destination.read_bytes()
    ).hexdigest()
    return evidence
