from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_syntax_glitch",
    ROOT / "scripts" / "native_syntax_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def motion_document() -> dict:
    return {
        "ffedit_version": "ffglitch-0.10.2",
        "filename": "fixture.avi",
        "sha1sum": "fixture",
        "features": ["mv"],
        "streams": [
            {
                "codec": "mpeg2video",
                "frames": [
                    {
                        "mv": {
                            "forward": [
                                [[2, 4], [8, -2]],
                                [[[1, 2], [3, 4], None, [5, 6]], None],
                            ],
                            "fcode": [4, 4],
                            "overflow": "warn",
                        }
                    },
                    {
                        "mv": {
                            "forward": [
                                [[6, 10], [12, -8]],
                                [[[7, 8], [9, 10], None, [11, 12]], None],
                            ],
                            "fcode": [4, 4],
                            "overflow": "warn",
                        }
                    },
                ],
            }
        ],
    }


def coefficient_document() -> dict:
    first = [80] + [index if index % 5 == 0 else 0 for index in range(1, 64)]
    second = [72] + [-index if index % 4 == 0 else 0 for index in range(1, 64)]
    return {
        "ffedit_version": "ffglitch-0.10.2",
        "filename": "fixture.avi",
        "sha1sum": "fixture",
        "features": ["q_dct"],
        "streams": [
            {
                "codec": "mpeg2video",
                "frames": [
                    {
                        "q_dct": {
                            "data": [[[first.copy(), second.copy()]]],
                            "v_count": [1, 1, 1],
                            "h_count": [1, 1, 1],
                        }
                    },
                    {
                        "q_dct": {
                            "data": [[[second.copy(), first.copy()]]],
                            "v_count": [1, 1, 1],
                            "h_count": [1, 1, 1],
                        }
                    },
                ],
            }
        ],
    }


def main() -> int:
    assert len(MODULE.EFFECTS) == 8
    for effect in MODULE.MOTION_EFFECTS:
        source = motion_document()
        baseline = copy.deepcopy(source)
        mutated, evidence = MODULE.mutate_document(
            source, effect, 1.0, 0x474C4943
        )
        assert mutated["sha1sum"] == baseline["sha1sum"]
        assert evidence["feature"] == "mv"
        assert evidence["changed_values"] > 0
        assert evidence["frames_with_changes"] > 0
        for frame in mutated["streams"][0]["frames"]:
            assert frame["mv"]["overflow"] == "truncate"
        duplicate, duplicate_evidence = MODULE.mutate_document(
            motion_document(), effect, 1.0, 0x474C4943
        )
        assert duplicate == mutated
        assert duplicate_evidence == evidence

    for effect in MODULE.COEFFICIENT_EFFECTS:
        source = coefficient_document()
        baseline_dc = [
            block[0]
            for frame in source["streams"][0]["frames"]
            for row in frame["q_dct"]["data"]
            for macroblock in row
            for block in macroblock
        ]
        mutated, evidence = MODULE.mutate_document(
            source, effect, 1.0, 0x474C4943
        )
        mutated_dc = [
            block[0]
            for frame in mutated["streams"][0]["frames"]
            for row in frame["q_dct"]["data"]
            for macroblock in row
            for block in macroblock
        ]
        assert mutated_dc == baseline_dc
        assert evidence["feature"] == "q_dct"
        assert evidence["changed_values"] > 0
        assert evidence["dc_coefficients_preserved"] is True

    unsupported = motion_document()
    unsupported["streams"][0]["codec"] = "h264"
    try:
        MODULE.mutate_document(
            unsupported,
            "compressed_motion_vector_mirror",
            1.0,
            1,
        )
    except MODULE.SyntaxMutationError as error:
        assert "MPEG-2 video only" in str(error)
    else:
        raise AssertionError("H.264 compressed-syntax mutation did not fail")

    print("PASS deterministic native compressed-syntax mutation helpers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
