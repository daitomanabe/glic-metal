from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "process_offline_packet_glitch",
    ROOT / "scripts" / "process_offline_packet_glitch.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def main() -> int:
    assert MODULE.CODECS == ("h264", "hevc", "av1", "vp9", "prores")
    assert MODULE.EFFECTS == (
        "packet_bit_rot",
        "gop_amputation",
        "packet_dropout_score",
        "timestamp_fracture",
        "nal_obu_surgery",
        "header_hallucination",
        "packet_transplant",
        "vp9_superframe_shuffle",
    )
    assert "prores" not in MODULE.SUPPORT["gop_amputation"]
    assert MODULE.SUPPORT["vp9_superframe_shuffle"] == {"vp9"}
    assert "noise=amount=" in MODULE.effect_bsf(
        "packet_bit_rot", "h264", 0.65, 7
    )
    assert MODULE.effect_bsf(
        "gop_amputation", "h264", 0.65, 7
    ) == "noise=drop='key*gt(n,0)'"
    assert "setts=pts=" in MODULE.effect_bsf(
        "timestamp_fracture", "hevc", 0.65, 7
    )
    assert MODULE.effect_bsf(
        "nal_obu_surgery", "h264", 0.65, 7
    ) == "filter_units=remove_types=1"
    assert MODULE.effect_bsf(
        "nal_obu_surgery", "hevc", 0.65, 7
    ) == "filter_units=remove_types=0|1"
    assert MODULE.effect_bsf(
        "header_hallucination", "av1", 0.65, 7
    ).startswith("av1_metadata=")
    assert MODULE.effect_bsf(
        "packet_transplant", "prores", 0.65, 7
    ) == ""
    assert MODULE.effect_bsf(
        "vp9_superframe_shuffle", "vp9", 0.65, 7
    ).startswith("vp9_superframe_split,")

    catalog = json.loads(
        (ROOT / "resources" / "offline-codec-effects.json").read_text()
    )
    assert catalog["schema"] == "glic-codec-effect-catalog-v1"
    catalog_support = {
        effect["name"]: set(effect["codecs"])
        for effect in catalog["offline_effects"]
    }
    assert catalog_support == MODULE.SUPPORT
    assert all(
        effect["execution_class"] == "offline"
        for effect in catalog["offline_effects"]
    )
    assert all(
        effect["realtime_certified"] is False
        for effect in catalog["offline_effects"]
    )
    print("PASS offline packet-glitch helpers and catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
