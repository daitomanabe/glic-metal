from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "process_structured_codec_glitch",
    ROOT / "scripts" / "process_structured_codec_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert MODULE.EFFECTS == (
        "av1_tile_group_surgery",
        "av1_film_grain_seed_surgery",
        "av1_reference_slot_surgery",
        "temporal_layer_dropout",
        "temporal_layer_reorder",
        "cross_stream_unit_transplant",
    )
    assert MODULE.SUPPORT["av1_tile_group_surgery"] == {"av1"}
    assert MODULE.SUPPORT["temporal_layer_dropout"] == {"hevc"}
    assert MODULE.SUPPORT["cross_stream_unit_transplant"] == {
        "h264",
        "hevc",
        "av1",
    }
    assert all(
        level
        not in {"placeholder", "decoded_reconstruction_proxy", "planned"}
        for level in MODULE.IMPLEMENTATION_LEVEL.values()
    )
    assert (
        MODULE.IMPLEMENTATION_LEVEL["av1_tile_group_surgery"]
        == "av1_trace_aligned_tile_group_obu_dropout"
    )
    options = MODULE.av1_encoder_options(
        "av1_film_grain_seed_surgery", 0.75, 30
    )
    assert "-svtav1-params" in options
    assert any(value.startswith("film-grain=") for value in options)
    assert MODULE.raw_extension("av1") == ".obu"
    assert MODULE.raw_muxer("hevc") == "hevc"
    print("PASS structured codec pipeline metadata and support matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
