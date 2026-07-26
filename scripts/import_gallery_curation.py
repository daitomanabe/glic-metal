#!/usr/bin/env python3
"""Import gallery decisions and generate the realtime SDK preset bank."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REVIEW = ROOT / "resources/glic-metal-gallery-review.json"
CANONICAL_ADOPTED = ROOT / "resources/glic-metal-adopted-presets.json"
SELECTED_PRESETS = ROOT / "resources/selected-presets.json"
GENERATED_CPP = ROOT / "src/generated_realtime_preset_bank.inc"

REALTIME_FAMILIES = {
    "original": "original",
    "spatial": "spatial",
    "codec_realtime": "codec",
}

SPATIAL_ENUMS = {
    "legacy_block": "GLIC_METAL_EFFECT_LEGACY_BLOCK",
    "line_tear": "GLIC_METAL_EFFECT_LINE_TEAR",
    "channel_shear": "GLIC_METAL_EFFECT_CHANNEL_SHEAR",
    "analog_sync": "GLIC_METAL_EFFECT_ANALOG_SYNC",
    "mirror_fold": "GLIC_METAL_EFFECT_MIRROR_FOLD",
    "edge_echo": "GLIC_METAL_EFFECT_EDGE_ECHO",
    "bitplane_dither": "GLIC_METAL_EFFECT_BITPLANE_DITHER",
    "wave_warp": "GLIC_METAL_EFFECT_WAVE_WARP",
    "poster_solar": "GLIC_METAL_EFFECT_POSTER_SOLAR",
    "tile_shuffle": "GLIC_METAL_EFFECT_TILE_SHUFFLE",
    "vertical_tear": "GLIC_METAL_EFFECT_VERTICAL_TEAR",
    "diagonal_slip": "GLIC_METAL_EFFECT_DIAGONAL_SLIP",
    "scanline_weave": "GLIC_METAL_EFFECT_SCANLINE_WEAVE",
    "quad_mirror": "GLIC_METAL_EFFECT_QUAD_MIRROR",
}

CODEC_EFFECT_ENUMS = {
    "qp_pump": "GLIC_CODEC_GLITCH_QP_PUMP",
    "bitrate_crush": "GLIC_CODEC_GLITCH_BITRATE_CRUSH",
    "slice_dropout": "GLIC_CODEC_GLITCH_SLICE_DROPOUT",
    "slice_transplant": "GLIC_CODEC_GLITCH_SLICE_TRANSPLANT",
    "pframe_loss": "GLIC_CODEC_GLITCH_PFRAME_LOSS",
    "idr_starvation": "GLIC_CODEC_GLITCH_IDR_STARVATION",
    "payload_xor": "GLIC_CODEC_GLITCH_PAYLOAD_XOR",
    "reference_timewarp": "GLIC_CODEC_GLITCH_REFERENCE_TIMEWARP",
    "codec_feedback": "GLIC_CODEC_GLITCH_CODEC_FEEDBACK",
    "generation_cascade": "GLIC_CODEC_GLITCH_GENERATION_CASCADE",
    "resolution_hop": "GLIC_CODEC_GLITCH_RESOLUTION_HOP",
    "chroma_codec_echo": "GLIC_CODEC_GLITCH_CHROMA_CODEC_ECHO",
    "temporal_polyphony": "GLIC_CODEC_GLITCH_TEMPORAL_POLYPHONY",
    "intra_cannibalism": "GLIC_CODEC_GLITCH_INTRA_CANNIBALISM",
    "residual_rift": "GLIC_CODEC_GLITCH_RESIDUAL_RIFT",
    "codec_grain_synth": "GLIC_CODEC_GLITCH_CODEC_GRAIN_SYNTH",
    "recursive_codec_skin": "GLIC_CODEC_GLITCH_RECURSIVE_CODEC_SKIN",
    "concealment_choreography": "GLIC_CODEC_GLITCH_CONCEALMENT_CHOREOGRAPHY",
    "dual_codec_crossbreed": "GLIC_CODEC_GLITCH_DUAL_CODEC_CROSSBREED",
    "codec_pingpong": "GLIC_CODEC_GLITCH_CODEC_PINGPONG",
    "gop_accordion": "GLIC_CODEC_GLITCH_GOP_ACCORDION",
    "bframe_braid": "GLIC_CODEC_GLITCH_BFRAME_BRAID",
    "plane_split_codec": "GLIC_CODEC_GLITCH_PLANE_SPLIT_CODEC",
    "roi_quality_islands": "GLIC_CODEC_GLITCH_ROI_QUALITY_ISLANDS",
    "codec_phase_mosaic": "GLIC_CODEC_GLITCH_CODEC_PHASE_MOSAIC",
    "encoder_hot_swap": "GLIC_CODEC_GLITCH_ENCODER_HOT_SWAP",
    "pts_rubberband": "GLIC_CODEC_GLITCH_PTS_RUBBERBAND",
    "bitrate_raster": "GLIC_CODEC_GLITCH_BITRATE_RASTER",
    "plane_time_split": "GLIC_CODEC_GLITCH_PLANE_TIME_SPLIT",
    "reference_atlas": "GLIC_CODEC_GLITCH_REFERENCE_ATLAS",
    "flow_lattice": "GLIC_CODEC_GLITCH_FLOW_LATTICE",
    "scan_order_fold": "GLIC_CODEC_GLITCH_SCAN_ORDER_FOLD",
    "regional_gop_clock": "GLIC_CODEC_GLITCH_REGIONAL_GOP_CLOCK",
    "entropy_feedback": "GLIC_CODEC_GLITCH_ENTROPY_FEEDBACK",
    "rolling_time_shutter": "GLIC_CODEC_GLITCH_ROLLING_TIME_SHUTTER",
    "asymmetric_plane_codec": "GLIC_CODEC_GLITCH_ASYMMETRIC_PLANE_CODEC",
}

CODEC_ENUMS = {
    "h264": "GLIC_CODEC_GLITCH_CODEC_H264",
    "hevc": "GLIC_CODEC_GLITCH_CODEC_HEVC",
    "prores_422": "GLIC_CODEC_GLITCH_CODEC_PRORES_422",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "review_json",
        nargs="?",
        type=Path,
        default=CANONICAL_REVIEW,
        help="Full gallery review export",
    )
    parser.add_argument(
        "--adopted-json",
        type=Path,
        default=CANONICAL_ADOPTED,
        help="Adopted-only gallery export",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write canonical curation files and generated SDK bank",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed generated files without changing them",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def normalized_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def validate(
    review: dict[str, Any], adopted_export: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if review.get("schema") != "glic-metal-gallery-review-v1":
        raise ValueError("review export schema mismatch")
    if adopted_export.get("schema") != review.get("schema"):
        raise ValueError("adopted export schema mismatch")
    if review.get("scope") != "all" or adopted_export.get("scope") != "adopted":
        raise ValueError("review export scopes must be all and adopted")

    items = review.get("items")
    adopted_items = adopted_export.get("items")
    if not isinstance(items, list) or not isinstance(adopted_items, list):
        raise ValueError("review exports must contain items arrays")
    keys = [item.get("key") for item in items]
    if any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("every review item must have a key")
    if len(keys) != len(set(keys)):
        raise ValueError("review keys must be unique")
    if any(
        item.get("decision") not in {"adopt", "reject", "pending"}
        for item in items
    ):
        raise ValueError("review contains an invalid decision")

    adopted = [item for item in items if item["decision"] == "adopt"]
    if [item["key"] for item in adopted] != [
        item.get("key") for item in adopted_items
    ]:
        raise ValueError("adopted export does not match full review decisions")
    counts = Counter(item["decision"] for item in items)
    expected_counts = {
        "adopted": counts["adopt"],
        "rejected": counts["reject"],
        "pending": counts["pending"],
    }
    if review.get("counts") != expected_counts:
        raise ValueError("full review counts do not match items")
    if adopted_export.get("counts") != expected_counts:
        raise ValueError("adopted export counts do not match full review")
    if any(
        item.get("technical_qa", {}).get("status") != "PASS"
        for item in adopted
    ):
        raise ValueError("every adopted item must pass technical QA")

    realtime = [item for item in adopted if item.get("realtime_certified") is True]
    if any(item.get("family") not in REALTIME_FAMILIES for item in realtime):
        raise ValueError("realtime item uses a non-realtime family")
    if len({item["algorithm_id"] for item in realtime}) != len(realtime):
        raise ValueError("realtime bank may contain only one variant per algorithm")
    return adopted, realtime


def runtime_name(item: dict[str, Any]) -> str:
    family = item["family"]
    if family == "codec_realtime":
        return f"codec__{item['codec']}__{item['effect']}"
    return f"{family}__{item['effect']}"


def selected_entry(item: dict[str, Any]) -> dict[str, Any]:
    family = item["family"]
    parameters = item["parameters"]
    category = REALTIME_FAMILIES[family]
    controls: dict[str, Any]
    if family == "original":
        controls = {
            "preset": item["effect"],
            "strength": parameters["strength"],
        }
    elif family == "spatial":
        controls = {
            "amount": parameters["amount"],
            "scale": parameters["scale"],
            "rate": parameters["rate"],
            "seed": parameters["seed"],
        }
    else:
        controls = {
            "amount": parameters["amount"],
            "rate": parameters["rate"],
            "feedback": parameters["feedback"],
            "cascade_generations": max(2, parameters["generations"]),
            "seed": parameters["seed"],
        }
    result: dict[str, Any] = {
        "name": runtime_name(item),
        "category": category,
        "effect": item["effect"],
        "controls": controls,
        "gallery_key": item["key"],
        "realtime_certified": True,
    }
    if family == "codec_realtime":
        result["codec"] = item["codec"]
    return result


def selected_payload(
    review: dict[str, Any],
    adopted: list[dict[str, Any]],
    realtime: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = [selected_entry(item) for item in realtime]
    category_counts = Counter(item["category"] for item in entries)
    return {
        "schema": "glic-metal-selected-presets-v2",
        "source": {
            "schema": review["schema"],
            "gallery_revision": review.get("gallery_revision"),
            "adopted_count": len(adopted),
            "offline_adopted_count": len(adopted) - len(realtime),
        },
        "realtime_only": True,
        "count": len(entries),
        "category_counts": {
            category: category_counts[category]
            for category in ("original", "spatial", "codec")
        },
        "mixed_glitch_patterns": entries,
    }


def cpp_float(value: Any) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text + "f"


def cpp_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def generated_cpp(entries: list[dict[str, Any]]) -> str:
    lines = [
        "// Generated by scripts/import_gallery_curation.py. Do not edit.",
        f"constexpr std::array<PresetRecord, {len(entries)}> kPresets{{{{",
    ]
    for entry in entries:
        controls = entry["controls"]
        if entry["category"] == "original":
            lines.append(
                "    original("
                f"{cpp_string(entry['name'])}, {cpp_string(entry['effect'])}, "
                f"{cpp_float(controls['strength'])}),"
            )
        elif entry["category"] == "spatial":
            effect_enum = SPATIAL_ENUMS[entry["effect"]]
            lines.extend(
                [
                    "    spatial("
                    f"{cpp_string(entry['name'])}, "
                    f"{cpp_string(entry['effect'])}, {effect_enum},",
                    "            "
                    f"{cpp_float(controls['amount'])}, "
                    f"{cpp_float(controls['scale'])}, "
                    f"{cpp_float(controls['rate'])}, "
                    f"UINT64_C({int(controls['seed'])})),",
                ]
            )
        else:
            effect_enum = CODEC_EFFECT_ENUMS[entry["effect"]]
            codec_enum = CODEC_ENUMS[entry["codec"]]
            lines.extend(
                [
                    "    codec("
                    f"{cpp_string(entry['name'])}, "
                    f"{cpp_string(entry['effect'])}, {effect_enum},",
                    "          "
                    f"{codec_enum}, {cpp_float(controls['amount'])}, "
                    f"{cpp_float(controls['rate'])}, "
                    f"{cpp_float(controls['feedback'])},",
                    "          "
                    f"{int(controls['cascade_generations'])}, "
                    f"UINT64_C({int(controls['seed'])})),",
                ]
            )
    lines.append("}};")
    return "\n".join(lines) + "\n"


def compare(path: Path, expected: str) -> None:
    actual = path.read_text(encoding="utf-8") if path.is_file() else None
    if actual != expected:
        raise ValueError(f"generated file is stale: {path}")


def main() -> int:
    args = parse_args()
    if args.write == args.check:
        raise SystemExit("choose exactly one of --write or --check")
    review = load_json(args.review_json)
    adopted_export = load_json(args.adopted_json)
    adopted, realtime = validate(review, adopted_export)
    selected = selected_payload(review, adopted, realtime)
    selected_text = normalized_json(selected)
    cpp_text = generated_cpp(selected["mixed_glitch_patterns"])

    if args.write:
        CANONICAL_REVIEW.write_text(
            normalized_json(review), encoding="utf-8"
        )
        CANONICAL_ADOPTED.write_text(
            normalized_json(adopted_export), encoding="utf-8"
        )
        SELECTED_PRESETS.write_text(selected_text, encoding="utf-8")
        GENERATED_CPP.write_text(cpp_text, encoding="utf-8")
    else:
        compare(CANONICAL_REVIEW, normalized_json(review))
        compare(CANONICAL_ADOPTED, normalized_json(adopted_export))
        compare(SELECTED_PRESETS, selected_text)
        compare(GENERATED_CPP, cpp_text)

    offline = len(adopted) - len(realtime)
    print(
        "PASS gallery curation: "
        f"{len(adopted)} adopted, {len(realtime)} realtime SDK presets, "
        f"{offline} offline-only adopted"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
