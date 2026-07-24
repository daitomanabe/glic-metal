"""Structured H.26x NAL and AV1 OBU parsing/mutation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import re


@dataclass
class AnnexBUnit:
    start_code: bytes
    payload: bytearray

    @property
    def h264_type(self) -> int:
        return self.payload[0] & 0x1F if self.payload else -1

    @property
    def hevc_type(self) -> int:
        return (self.payload[0] >> 1) & 0x3F if len(self.payload) >= 2 else -1

    @property
    def hevc_temporal_id(self) -> int:
        return (self.payload[1] & 0x07) - 1 if len(self.payload) >= 2 else -1


@dataclass
class Av1Obu:
    raw: bytearray
    obu_type: int
    extension_flag: bool
    temporal_id: int
    spatial_id: int
    payload_offset: int
    payload_size: int


@dataclass
class TraceField:
    position: int
    name: str
    bits: str
    value: int

    @property
    def end(self) -> int:
        return self.position + len(self.bits)


@dataclass
class TracedObu:
    obu_type: int = -1
    fields: list[TraceField] = field(default_factory=list)
    tile_group_start_bit: int | None = None


START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")
TRACE_FIELD = re.compile(
    r"\]\s+(\d+)\s+(.+?)\s+([01]+)\s+=\s+(-?\d+)\s*$"
)


def split_annexb(data: bytes) -> list[AnnexBUnit]:
    matches = list(START_CODE.finditer(data))
    units: list[AnnexBUnit] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(data)
        payload = data[match.end() : end]
        if payload:
            units.append(
                AnnexBUnit(
                    start_code=data[match.start() : match.end()],
                    payload=bytearray(payload),
                )
            )
    return units


def join_annexb(units: list[AnnexBUnit]) -> bytes:
    return b"".join(unit.start_code + unit.payload for unit in units)


def decode_leb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for byte_index in range(8):
        if offset + byte_index >= len(data):
            raise ValueError("truncated LEB128")
        byte = data[offset + byte_index]
        value |= (byte & 0x7F) << (byte_index * 7)
        if byte & 0x80 == 0:
            return value, byte_index + 1
    raise ValueError("LEB128 exceeds eight bytes")


def parse_av1_obus(data: bytes) -> list[Av1Obu]:
    result: list[Av1Obu] = []
    offset = 0
    while offset < len(data):
        start = offset
        header = data[offset]
        offset += 1
        if header & 0x80:
            raise ValueError(f"forbidden AV1 OBU bit at byte {start}")
        obu_type = (header >> 3) & 0x0F
        extension = bool(header & 0x04)
        has_size = bool(header & 0x02)
        if not has_size:
            raise ValueError("low-overhead AV1 OBU has no size field")
        temporal_id = 0
        spatial_id = 0
        if extension:
            if offset >= len(data):
                raise ValueError("truncated AV1 OBU extension")
            extension_header = data[offset]
            offset += 1
            temporal_id = (extension_header >> 5) & 0x07
            spatial_id = (extension_header >> 3) & 0x03
        payload_size, size_length = decode_leb128(data, offset)
        offset += size_length
        payload_offset = offset - start
        end = offset + payload_size
        if end > len(data):
            raise ValueError("truncated AV1 OBU payload")
        result.append(
            Av1Obu(
                raw=bytearray(data[start:end]),
                obu_type=obu_type,
                extension_flag=extension,
                temporal_id=temporal_id,
                spatial_id=spatial_id,
                payload_offset=payload_offset,
                payload_size=payload_size,
            )
        )
        offset = end
    return result


def join_av1_obus(units: list[Av1Obu]) -> bytes:
    return b"".join(unit.raw for unit in units)


def parse_trace_headers(text: str) -> list[TracedObu]:
    result: list[TracedObu] = []
    current: TracedObu | None = None
    for line in text.splitlines():
        match = TRACE_FIELD.search(line)
        if match:
            position = int(match.group(1))
            name = match.group(2).strip()
            bits = match.group(3)
            value = int(match.group(4))
            if name == "obu_forbidden_bit" and position == 0:
                current = TracedObu()
                result.append(current)
            if current is None:
                continue
            trace_field = TraceField(position, name, bits, value)
            current.fields.append(trace_field)
            if name == "obu_type":
                current.obu_type = value
            continue
        if current is not None and line.rstrip().endswith("Tile Group"):
            last_end = max((item.end for item in current.fields), default=0)
            current.tile_group_start_bit = (last_end + 7) // 8 * 8
    return result


def align_traced_obus(
    obus: list[Av1Obu], traced: list[TracedObu]
) -> list[TracedObu | None]:
    aligned: list[TracedObu | None] = []
    cursor = 0
    for obu in obus:
        match: TracedObu | None = None
        while cursor < len(traced):
            candidate = traced[cursor]
            cursor += 1
            if candidate.obu_type == obu.obu_type:
                match = candidate
                break
        aligned.append(match)
    return aligned


def write_unsigned_bits(
    data: bytearray, position: int, width: int, value: int
) -> None:
    if position < 0 or width < 1 or position + width > len(data) * 8:
        raise ValueError("bit range is outside the unit")
    if value < 0 or value >= 1 << width:
        raise ValueError("value does not fit bit range")
    for offset in range(width):
        bit_position = position + offset
        byte_index = bit_position // 8
        bit_index = 7 - bit_position % 8
        mask = 1 << bit_index
        bit = (value >> (width - offset - 1)) & 1
        if bit:
            data[byte_index] |= mask
        else:
            data[byte_index] &= ~mask


def mutate_av1_field_syntax(
    data: bytes,
    trace_text: str,
    effect: str,
    amount: float,
    seed: int,
) -> tuple[bytes, dict]:
    obus = parse_av1_obus(data)
    aligned = align_traced_obus(obus, parse_trace_headers(trace_text))
    rng = random.Random(seed)
    changed_fields = 0
    changed_bytes = 0
    matched_units = 0
    tile_ordinal = 0
    dropped_units: set[int] = set()

    for unit_index, (obu, traced) in enumerate(zip(obus, aligned)):
        if traced is None:
            continue
        if effect == "av1_reference_slot_surgery":
            fields = [
                item
                for item in traced.fields
                if item.name.startswith("ref_frame_idx[")
            ]
            if fields:
                matched_units += 1
            for field_index, item in enumerate(fields):
                if rng.random() > 0.18 + amount * 0.68:
                    continue
                replacement = (
                    item.value
                    + 1
                    + ((seed + unit_index + field_index) % 7)
                ) % 8
                write_unsigned_bits(
                    obu.raw, item.position, len(item.bits), replacement
                )
                changed_fields += 1
        elif effect == "av1_film_grain_seed_surgery":
            fields = [
                item
                for item in traced.fields
                if item.name == "grain_seed"
            ]
            if fields:
                matched_units += 1
            for item in fields:
                mask = (seed ^ (unit_index * 0x9E37)) & 0xFFFF
                replacement = item.value ^ mask
                write_unsigned_bits(
                    obu.raw, item.position, len(item.bits), replacement
                )
                changed_fields += 1
        elif effect == "av1_tile_group_surgery":
            if obu.obu_type == 4:
                start = obu.payload_offset
            elif obu.obu_type == 6 and traced.tile_group_start_bit is not None:
                start = traced.tile_group_start_bit // 8
            else:
                continue
            current_tile = tile_ordinal
            tile_ordinal += 1
            if start >= len(obu.raw):
                continue
            matched_units += 1
            period = max(2, round(6 - amount * 4))
            if current_tile == 0 or current_tile % period != seed % period:
                continue
            dropped_units.add(unit_index)
        else:
            raise ValueError(f"unsupported AV1 syntax effect: {effect}")

    if matched_units == 0:
        raise RuntimeError(f"{effect} found no matching AV1 syntax")
    if changed_fields == 0 and changed_bytes == 0 and not dropped_units:
        raise RuntimeError(f"{effect} made no structured mutation")
    output_obus = [
        obu for index, obu in enumerate(obus) if index not in dropped_units
    ]
    return join_av1_obus(output_obus), {
        "parsed_obus": len(obus),
        "trace_obus": len(parse_trace_headers(trace_text)),
        "matched_units": matched_units,
        "changed_fields": changed_fields,
        "changed_bytes": changed_bytes,
        "dropped_tile_group_obus": len(dropped_units),
        "trace_alignment_complete": all(item is not None for item in aligned),
    }


def mutate_temporal_layers(
    units: list[AnnexBUnit],
    effect: str,
    amount: float,
    seed: int,
) -> tuple[list[AnnexBUnit], dict]:
    temporal = [
        (index, unit)
        for index, unit in enumerate(units)
        if 0 <= unit.hevc_type <= 31 and unit.hevc_temporal_id > 0
    ]
    if not temporal:
        raise RuntimeError("HEVC stream has no enhancement temporal-layer VCL units")
    highest = max(unit.hevc_temporal_id for _, unit in temporal)
    candidates = [
        (index, unit)
        for index, unit in temporal
        if unit.hevc_temporal_id == highest
    ]
    selected_indices = {
        index
        for order, (index, _) in enumerate(candidates)
        if (order + seed) % max(2, round(5 - amount * 3)) == 0
    }
    if not selected_indices:
        selected_indices.add(candidates[0][0])

    if effect == "temporal_layer_dropout":
        output = [
            unit for index, unit in enumerate(units) if index not in selected_indices
        ]
    elif effect == "temporal_layer_reorder":
        output = list(units)
        payloads = [output[index].payload for index in sorted(selected_indices)]
        payloads.reverse()
        for index, payload in zip(sorted(selected_indices), payloads):
            output[index] = AnnexBUnit(output[index].start_code, bytearray(payload))
    else:
        raise ValueError(f"unsupported temporal layer effect: {effect}")
    return output, {
        "enhancement_vcl_units": len(temporal),
        "highest_temporal_id": highest,
        "selected_units": len(selected_indices),
    }


def transplant_annexb_units(
    target: list[AnnexBUnit],
    donor: list[AnnexBUnit],
    codec: str,
    amount: float,
    seed: int,
) -> tuple[list[AnnexBUnit], dict]:
    if codec == "h264":
        target_candidates = [
            index for index, unit in enumerate(target) if unit.h264_type in {1, 5}
        ]
        donor_candidates = [
            unit for unit in donor if unit.h264_type in {1, 5}
        ]
    elif codec == "hevc":
        target_candidates = [
            index for index, unit in enumerate(target) if 0 <= unit.hevc_type <= 31
        ]
        donor_candidates = [
            unit for unit in donor if 0 <= unit.hevc_type <= 31
        ]
    else:
        raise ValueError(f"unsupported Annex-B transplant codec: {codec}")
    if not target_candidates or not donor_candidates:
        raise RuntimeError("no compatible VCL units for transplant")
    stride = max(2, round(8 - amount * 6))
    selected = target_candidates[(seed % stride) :: stride]
    if not selected:
        selected = [target_candidates[0]]
    output = list(target)
    for order, target_index in enumerate(selected):
        donor_unit = donor_candidates[(order + seed) % len(donor_candidates)]
        output[target_index] = AnnexBUnit(
            output[target_index].start_code,
            bytearray(donor_unit.payload),
        )
    return output, {
        "target_vcl_units": len(target_candidates),
        "donor_vcl_units": len(donor_candidates),
        "transplanted_units": len(selected),
    }


def transplant_av1_units(
    target: list[Av1Obu],
    donor: list[Av1Obu],
    amount: float,
    seed: int,
) -> tuple[list[Av1Obu], dict]:
    mutable_types = {3, 4, 6}
    target_candidates = [
        index for index, unit in enumerate(target) if unit.obu_type in mutable_types
    ]
    donor_candidates = [
        unit for unit in donor if unit.obu_type in mutable_types
    ]
    if not target_candidates or not donor_candidates:
        raise RuntimeError("no compatible AV1 frame/tile OBUs for transplant")
    stride = max(2, round(8 - amount * 6))
    selected = target_candidates[(seed % stride) :: stride]
    if not selected:
        selected = [target_candidates[0]]
    output = list(target)
    for order, target_index in enumerate(selected):
        desired_type = output[target_index].obu_type
        compatible = [
            unit for unit in donor_candidates if unit.obu_type == desired_type
        ]
        if not compatible:
            continue
        replacement = compatible[(order + seed) % len(compatible)]
        output[target_index] = Av1Obu(
            raw=bytearray(replacement.raw),
            obu_type=replacement.obu_type,
            extension_flag=replacement.extension_flag,
            temporal_id=replacement.temporal_id,
            spatial_id=replacement.spatial_id,
            payload_offset=replacement.payload_offset,
            payload_size=replacement.payload_size,
        )
    return output, {
        "target_frame_obus": len(target_candidates),
        "donor_frame_obus": len(donor_candidates),
        "transplanted_units": len(selected),
    }
