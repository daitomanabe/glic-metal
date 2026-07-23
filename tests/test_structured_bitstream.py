from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import structured_bitstream as syntax  # noqa: E402


def obu(obu_type: int, payload: bytes, extension: int | None = None) -> bytes:
    header = (obu_type << 3) | 0x02 | (0x04 if extension is not None else 0)
    size = len(payload)
    assert size < 128
    return bytes([header]) + (
        bytes([extension]) if extension is not None else b""
    ) + bytes([size]) + payload


def main() -> int:
    annexb = (
        b"\x00\x00\x00\x01\x67\x11\x22"
        b"\x00\x00\x01\x65\x33\x44"
    )
    units = syntax.split_annexb(annexb)
    assert [unit.h264_type for unit in units] == [7, 5]
    assert syntax.join_annexb(units) == annexb

    stream = obu(2, b"") + obu(6, b"\x12\x34\x56") + obu(4, b"\xaa\xbb")
    parsed = syntax.parse_av1_obus(stream)
    assert [unit.obu_type for unit in parsed] == [2, 6, 4]
    assert syntax.join_av1_obus(parsed) == stream

    extended = syntax.parse_av1_obus(obu(6, b"\x00", 0b10101000))[0]
    assert extended.temporal_id == 5
    assert extended.spatial_id == 1

    value = bytearray(b"\x00\x00")
    syntax.write_unsigned_bits(value, 3, 5, 0b10101)
    assert value == bytearray(b"\x15\x00")

    trace = """
[trace_headers @ 0x1] OBU header
[trace_headers @ 0x1] 0 obu_forbidden_bit 0 = 0
[trace_headers @ 0x1] 1 obu_type 0110 = 6
[trace_headers @ 0x1] 5 obu_extension_flag 0 = 0
[trace_headers @ 0x1] 6 obu_has_size_field 1 = 1
[trace_headers @ 0x1] 7 obu_reserved_1bit 0 = 0
[trace_headers @ 0x1] 8 obu_size 00000011 = 3
[trace_headers @ 0x1] 16 ref_frame_idx[0] 001 = 1
[trace_headers @ 0x1] 19 ref_frame_idx[1] 010 = 2
[trace_headers @ 0x1] Tile Group
"""
    traced = syntax.parse_trace_headers(trace)
    assert len(traced) == 1
    assert traced[0].obu_type == 6
    assert traced[0].tile_group_start_bit == 24
    aligned = syntax.align_traced_obus([parsed[1]], traced)
    assert aligned[0] is traced[0]

    hevc_units = [
        syntax.AnnexBUnit(b"\x00\x00\x01", bytearray([2, 1])),
        syntax.AnnexBUnit(b"\x00\x00\x01", bytearray([2, 2])),
        syntax.AnnexBUnit(b"\x00\x00\x01", bytearray([2, 3])),
    ]
    dropped, report = syntax.mutate_temporal_layers(
        hevc_units, "temporal_layer_dropout", 0.8, 1
    )
    assert len(dropped) < len(hevc_units)
    assert report["highest_temporal_id"] == 2

    print("PASS structured NAL/OBU parsing and mutation helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
