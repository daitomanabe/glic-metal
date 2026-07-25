from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "x265_analysis_glitch",
    ROOT / "scripts" / "x265_analysis_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def analysis_header(*, cutree: int = 0) -> bytes:
    return struct.pack(
        "<20i",
        0,  # right offset
        0,  # bottom offset
        0,  # intra refresh
        3,  # references
        250,
        12,
        1,  # open GOP
        4,  # B frames
        1,  # B pyramid
        8,  # minimum CU
        20,  # lookahead
        0,
        0,
        0,  # CTU distortion
        0,
        10,  # analysis reuse
        cutree,
        320,
        180,
        64,
    )


def frame_header(
    size: int,
    depth_bytes: int,
    poc: int,
    slice_type: int,
    num_cus: int = 1,
    num_partitions: int = 4,
) -> bytes:
    return struct.pack(
        "<IIiiiqII",
        size,
        depth_bytes,
        poc,
        slice_type,
        0,
        100,
        num_cus,
        num_partitions,
    )


def intra_frame() -> bytes:
    depth_bytes = 2
    payload = bytes([0, 1]) * 3 + bytes([0, 1, 2, 3])
    size = MODULE.FRAME_HEADER_BYTES + len(payload)
    return frame_header(size, depth_bytes, 0, MODULE.X265_TYPE_IDR) + payload


def p_frame() -> bytes:
    depth_bytes = 2
    payload = bytearray(MODULE.WEIGHT_PARAM_BYTES * 3)
    payload.extend(bytes([0, 0]))  # depth
    payload.extend(bytes([1, 1]))  # mode
    payload.extend(bytes([0, 0]))  # partition
    payload.extend(bytes([0, 0]))  # merge
    payload.extend(bytes([1, 1]))  # inter direction
    payload.extend(bytes([0, 0]))  # chroma mode
    payload.extend(bytes([0, 0]))  # MVP
    payload.extend(struct.pack("<bb", 0, -1))
    payload.extend(struct.pack("<iiii", 3, 4, 9, 10))
    payload.extend(bytes([0, 1, 2, 3]))  # luma modes
    size = MODULE.FRAME_HEADER_BYTES + len(payload)
    return frame_header(size, depth_bytes, 1, MODULE.X265_TYPE_P) + payload


def b_frame() -> bytes:
    depth_bytes = 1
    payload = bytearray(MODULE.WEIGHT_PARAM_BYTES * 3 * 2)
    payload.extend(bytes([0]))  # depth
    payload.extend(bytes([1]))  # mode
    payload.extend(bytes([0]))  # partition
    payload.extend(bytes([0]))  # merge
    payload.extend(bytes([3]))  # inter direction
    for x, y in ((5, 6), (-7, 8)):
        payload.extend(bytes([0]))  # MVP
        payload.extend(struct.pack("<b", 0))
        payload.extend(struct.pack("<ii", x, y))
    size = MODULE.FRAME_HEADER_BYTES + len(payload)
    return frame_header(size, depth_bytes, 2, MODULE.X265_TYPE_B) + payload


def fixture() -> bytes:
    return analysis_header() + intra_frame() + p_frame() + b_frame()


def main() -> int:
    source = fixture()
    header, planes, frame_count = MODULE.parse_motion_planes(source)
    assert header.analysis_reuse_level == 10
    assert header.width == 320 and header.height == 180
    assert frame_count == 3
    assert len(planes) == 3

    mutated, evidence = MODULE.mutate_analysis_bytes(
        source,
        "compressed_motion_vector_mirror",
        1.0,
        0x474C4943,
    )
    assert source != mutated
    assert evidence["total_vector_candidates"] == 3
    assert evidence["selected_vector_candidates"] == 3
    assert evidence["changed_values"] == 3
    assert evidence["frames_with_changes"] == 2
    assert evidence["valid_reference_vectors_only"] is True
    assert (
        evidence["implementation_level"]
        == "native_hevc_x265_analysis_motion_vector_injection_before_cabac"
    )
    for plane, expected in zip(planes, ((-3, 4), (-5, 6), (7, 8))):
        assert struct.unpack_from("<ii", mutated, plane.mv_offset) == expected

    duplicate, duplicate_evidence = MODULE.mutate_analysis_bytes(
        source,
        "compressed_motion_vector_mirror",
        1.0,
        0x474C4943,
    )
    assert duplicate == mutated
    assert duplicate_evidence == evidence

    for effect in (
        "compressed_motion_vector_vortex",
        "compressed_motion_vector_quantizer",
        "compressed_motion_vector_freeze",
    ):
        effect_output, effect_evidence = MODULE.mutate_analysis_bytes(
            source, effect, 1.0, 0x474C4943
        )
        assert effect_output != source, effect
        assert effect_evidence["changed_values"] > 0, effect

    invalid = analysis_header(cutree=1) + intra_frame() + p_frame()
    try:
        MODULE.parse_motion_planes(invalid)
    except MODULE.X265AnalysisError as error:
        assert "--no-cutree" in str(error)
    else:
        raise AssertionError("CU-tree analysis input did not fail closed")

    print("PASS x265 4.2 HEVC analysis MV mutation helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
