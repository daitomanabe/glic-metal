#!/usr/bin/env python3
"""Render and publish a deterministic gallery of every GLIC Metal algorithm.

The gallery treats an algorithm as an executable effect name. Codec generation
effects are expanded per codec because those implementations use materially
different encoders. The adopted original-style presets are included as named
recipes, while duplicate spatial/codec recipes in selected-presets.json are
represented by their canonical effect entries.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import html
from itertools import combinations
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "assets" / "test-video.mp4"
DEFAULT_OUTPUT = ROOT / "output" / "glitch-algorithm-gallery"
DEFAULT_CATALOG = ROOT / "resources" / "glitch-gallery-presets.json"
WIDTH = 480
HEIGHT = 270
FPS = 24
MAX_FRAMES = 120
VARIANT_NAMES = ("structure", "rhythm", "saturation")

SPATIAL_EFFECTS = (
    "legacy_block",
    "line_tear",
    "channel_shear",
    "analog_sync",
    "mirror_fold",
    "edge_echo",
    "bitplane_dither",
    "wave_warp",
    "poster_solar",
    "tile_shuffle",
    "vertical_tear",
    "diagonal_slip",
    "scanline_weave",
    "quad_mirror",
)

PROFILE_LABELS = {
    "structure": {
        "ja": "STRUCTURE / 構造",
        "en": "STRUCTURE",
        "description_ja": "形状と圧縮構造を残しながら、局所的な破綻を見せる設定。",
        "description_en": "Local disruption that preserves the underlying form and codec structure.",
    },
    "rhythm": {
        "ja": "RHYTHM / 律動",
        "en": "RHYTHM",
        "description_ja": "時間変化と反復が読み取れるよう、速度と履歴を強調した設定。",
        "description_en": "A temporal setting that emphasizes modulation, repetition, and history.",
    },
    "saturation": {
        "ja": "SATURATION / 飽和",
        "en": "SATURATION",
        "description_ja": "デコード可能性を保つ範囲で、効果量を強く押し込んだ設定。",
        "description_en": "A high-intensity setting pushed to the edge of reliable decoding.",
    },
}

FAMILY_INFO = {
    "original": ("Original GLIC", "採用済み原作レシピ"),
    "spatial": ("Metal Spatial", "Metal空間グリッチ"),
    "codec_realtime": ("VideoToolbox Realtime", "VideoToolboxリアルタイム"),
    "codec_lab": ("Codec Reconstruction", "codec再構成"),
    "native_syntax": ("Compressed Syntax", "圧縮syntax直接操作"),
    "offline_packet": ("Packet Damage", "packet破壊"),
    "structured": ("Structured Bitstream", "構造化bitstream"),
    "transport": ("Transport", "transport"),
    "metadata": ("Metadata", "metadata"),
    "generation": ("Codec Generations", "codec世代劣化"),
}


@dataclass(frozen=True)
class RenderTask:
    algorithm: dict[str, Any]
    preset: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.algorithm['id']}--{self.preset['id']}"

    @property
    def slug(self) -> str:
        return slugify(self.key)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def slugify(value: str) -> str:
    result = []
    for character in value.lower():
        result.append(character if character.isalnum() else "-")
    return "-".join(filter(None, "".join(result).split("-")))


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return round(max(minimum, min(maximum, value)), 3)


def implementation_for(
    family: str,
    effect: str,
    codec: str | None,
    codec_catalog: dict[str, Any],
) -> str:
    if family == "original":
        return "metal_original_style_algorithmic_core"
    if family == "spatial":
        return "metal_realtime_compute"
    if family == "codec_realtime":
        return "videotoolbox_clean_decode_plus_metal_reconstruction"
    if family == "codec_lab":
        for section in ("syntax_lab", "analysis_and_search"):
            level = codec_catalog[section]["implementation_levels"].get(effect)
            if level:
                return level
    if family == "native_syntax":
        return codec_catalog["native_compressed_syntax_lab"][
            "implementation_levels"
        ][effect]
    if family == "offline_packet":
        return "isolated_compressed_packet_mutation_and_salvage"
    if family == "structured":
        return codec_catalog["structured_bitstream_lab"][
            "implementation_levels"
        ][effect]
    if family == "transport":
        return codec_catalog["transport_lab"]["implementation_levels"][effect]
    if family == "metadata":
        return codec_catalog["metadata_lab"]["implementation_levels"][effect]
    if family == "generation" and codec:
        return codec_catalog["generation_codecs"]["implementation_levels"][codec]
    raise KeyError(f"no implementation level for {family}:{effect}:{codec}")


def effect_profile(effect: str, family: str, index: int) -> dict[str, Any]:
    amount = (0.34, 0.62, 0.84)[index]
    rate = (0.18, 0.54, 0.86)[index]
    feedback = (0.12, 0.43, 0.72)[index]
    scale = (0.24, 0.52, 0.80)[index]
    strength = (0.68, 1.08, 1.58)[index]
    generations = (1, 2, 3)[index]
    lower = effect.lower()
    rationale = "balanced"

    if any(word in lower for word in ("dropout", "amputation", "rot", "puncture")):
        amount = (0.14, 0.30, 0.50)[index]
        feedback = (0.08, 0.28, 0.48)[index]
        rationale = "decode_safe_damage"
    elif any(word in lower for word in ("surgery", "fracture", "hallucination")):
        amount = (0.18, 0.38, 0.62)[index]
        rationale = "structured_damage"
    elif any(word in lower for word in ("feedback", "echo", "recursive", "resonance")):
        amount = (0.38, 0.62, 0.82)[index]
        feedback = (0.30, 0.62, 0.86)[index]
        rationale = "history_dominant"
    elif any(word in lower for word in ("freeze", "starvation", "timewarp", "time_split")):
        amount = (0.36, 0.64, 0.86)[index]
        rate = (0.10, 0.38, 0.72)[index]
        feedback = (0.22, 0.54, 0.80)[index]
        rationale = "temporal_hold"
    elif any(word in lower for word in ("quant", "bitrate", "qp_", "grain")):
        amount = (0.40, 0.68, 0.92)[index]
        rationale = "quantization_range"
    elif any(word in lower for word in ("chroma", "plane", "color", "hdr", "vui")):
        amount = (0.38, 0.66, 0.90)[index]
        rate = (0.20, 0.62, 0.94)[index]
        rationale = "color_plane_range"
    elif any(word in lower for word in ("vector", "flow", "motion", "rift")):
        amount = (0.36, 0.64, 0.88)[index]
        rate = (0.16, 0.48, 0.78)[index]
        rationale = "motion_field_range"
    elif any(word in lower for word in ("transplant", "cross", "ensemble", "atlas")):
        amount = (0.30, 0.58, 0.82)[index]
        feedback = (0.18, 0.48, 0.76)[index]
        rationale = "source_mixing_range"

    # A small deterministic offset prevents unrelated algorithms from receiving
    # numerically identical recipes while keeping the three semantic tiers.
    jitter = ((stable_int(f"{family}:{effect}") % 9) - 4) * 0.006
    if index == 1:
        jitter *= -0.5
    parameters = {
        "amount": clamp(amount + jitter),
        "rate": clamp(rate - jitter),
        "feedback": clamp(feedback + jitter * 0.5),
        "scale": clamp(scale - jitter),
        "strength": clamp(strength + jitter, 0.0, 2.0),
        # The gallery mix is deliberately explicit. Some original GLIC recipes
        # quantize multiple strength values to the same codec state, while some
        # syntax mutations are necessarily discrete. A dry/wet stage preserves
        # the actual algorithm output and guarantees that the three documented
        # looks remain visually distinguishable.
        "wet_mix": (0.62, 0.82, 1.0)[index],
        "generations": generations,
        "seed": stable_int(f"glic-gallery:{family}:{effect}:{index}"),
    }
    return {"parameters": parameters, "rationale": rationale}


def preset_rows(effect: str, family: str) -> list[dict[str, Any]]:
    result = []
    for index, name in enumerate(VARIANT_NAMES):
        profile = effect_profile(effect, family, index)
        result.append(
            {
                "id": name,
                "label_ja": PROFILE_LABELS[name]["ja"],
                "label_en": PROFILE_LABELS[name]["en"],
                "description_ja": PROFILE_LABELS[name]["description_ja"],
                "description_en": PROFILE_LABELS[name]["description_en"],
                "rationale": profile["rationale"],
                "parameters": profile["parameters"],
            }
        )
    return result


def algorithm_row(
    *,
    family: str,
    effect: str,
    codec_catalog: dict[str, Any],
    codec: str | None = None,
    display_name: str | None = None,
    realtime: bool = False,
) -> dict[str, Any]:
    identifier = f"{family}:{effect}" if codec is None else f"{family}:{codec}:{effect}"
    presets = preset_rows(effect, family)
    if family == "generation" and codec == "av2":
        # One official AVM encode/decode pass already costs several minutes for
        # this five-second gallery source. The three looks remain distinct by
        # amount/rate/feedback; generation depth is held at one so every AV2
        # algorithm can finish without substituting a different codec.
        for preset in presets:
            preset["parameters"]["generations"] = 1
    return {
        "id": identifier,
        "family": family,
        "family_label_en": FAMILY_INFO[family][0],
        "family_label_ja": FAMILY_INFO[family][1],
        "effect": effect,
        "codec": codec,
        "display_name": display_name or effect.replace("_", " "),
        "implementation_level": implementation_for(
            family, effect, codec, codec_catalog
        ),
        "realtime_certified": realtime,
        "presets": presets,
    }


def build_catalog() -> dict[str, Any]:
    selected = read_json(ROOT / "resources" / "selected-presets.json")[
        "mixed_glitch_patterns"
    ]
    integration = read_json(ROOT / "resources" / "integration-manifest.json")
    codec_catalog = read_json(ROOT / "resources" / "codec-lab-effects.json")
    packet_catalog = read_json(ROOT / "resources" / "offline-codec-effects.json")
    algorithms: list[dict[str, Any]] = []

    for item in selected:
        if item["category"] != "original":
            continue
        algorithms.append(
            algorithm_row(
                family="original",
                effect=item["effect"],
                codec_catalog=codec_catalog,
                display_name=f"GLIC / {item['effect']}",
                realtime=True,
            )
        )

    for effect in SPATIAL_EFFECTS:
        algorithms.append(
            algorithm_row(
                family="spatial",
                effect=effect,
                codec_catalog=codec_catalog,
                realtime=True,
            )
        )

    codec_formats = ("h264", "hevc", "prores_422")
    for index, effect in enumerate(integration["lanes"]["codec"]["effect_names"]):
        algorithms.append(
            algorithm_row(
                family="codec_realtime",
                effect=effect,
                codec=codec_formats[index % len(codec_formats)],
                codec_catalog=codec_catalog,
                realtime=True,
            )
        )

    codec_lab_codecs = {
        "av1_film_grain_instrument": "av1",
        "av2_optical_flow_wound": "av2",
        "decoder_fingerprint_ensemble": "hevc",
        "cross_codec_chain": "h264",
        "decoder_disagreement_amplifier": "hevc",
    }
    codec_lab_effects = [
        *codec_catalog["syntax_lab"]["effect_names"],
        *(
            effect
            for effect in codec_catalog["analysis_and_search"]["effect_names"]
            if effect != "evolutionary_codec_search"
        ),
    ]
    for effect in codec_lab_effects:
        algorithms.append(
            algorithm_row(
                family="codec_lab",
                effect=effect,
                codec=codec_lab_codecs.get(effect, "h264"),
                codec_catalog=codec_catalog,
            )
        )

    for effect in codec_catalog["native_compressed_syntax_lab"]["effect_names"]:
        algorithms.append(
            algorithm_row(
                family="native_syntax",
                effect=effect,
                codec="mpeg2",
                codec_catalog=codec_catalog,
            )
        )

    packet_codecs = {
        "packet_bit_rot": "h264",
        "gop_amputation": "hevc",
        "packet_dropout_score": "av1",
        "timestamp_fracture": "h264",
        "nal_obu_surgery": "av1",
        "header_hallucination": "vp9",
        "packet_transplant": "hevc",
        "vp9_superframe_shuffle": "vp9",
    }
    for item in packet_catalog["offline_effects"]:
        effect = item["name"]
        algorithms.append(
            algorithm_row(
                family="offline_packet",
                effect=effect,
                codec=packet_codecs[effect],
                codec_catalog=codec_catalog,
            )
        )

    structured_codecs = {
        "av1_tile_group_surgery": "av1",
        "av1_film_grain_seed_surgery": "av1",
        "av1_reference_slot_surgery": "av1",
        "temporal_layer_dropout": "hevc",
        "temporal_layer_reorder": "hevc",
        "cross_stream_unit_transplant": "h264",
    }
    for effect in codec_catalog["structured_bitstream_lab"]["effect_names"]:
        algorithms.append(
            algorithm_row(
                family="structured",
                effect=effect,
                codec=structured_codecs[effect],
                codec_catalog=codec_catalog,
            )
        )

    for effect in codec_catalog["transport_lab"]["effect_names"]:
        algorithms.append(
            algorithm_row(
                family="transport",
                effect=effect,
                codec_catalog=codec_catalog,
            )
        )

    for effect in codec_catalog["metadata_lab"]["effect_names"]:
        algorithms.append(
            algorithm_row(
                family="metadata",
                effect=effect,
                codec="hevc" if effect == "hdr_metadata_pulse" else "h264",
                codec_catalog=codec_catalog,
            )
        )

    generation = codec_catalog["generation_codecs"]
    for codec in generation["codecs"]:
        for effect in generation["effect_names"]:
            algorithms.append(
                algorithm_row(
                    family="generation",
                    effect=effect,
                    codec=codec,
                    display_name=f"{codec.upper()} / {effect.replace('_', ' ')}",
                    codec_catalog=codec_catalog,
                )
            )

    ids = [row["id"] for row in algorithms]
    if len(ids) != len(set(ids)):
        raise RuntimeError("gallery algorithm identifiers are not unique")
    result = {
        "schema": "glic-glitch-gallery-presets-v1",
        "definition": (
            "Every executable named effect is one algorithm. Generation effects "
            "are expanded per codec because each encoder implementation differs."
        ),
        "source_catalogs": [
            "resources/selected-presets.json",
            "resources/integration-manifest.json",
            "resources/offline-codec-effects.json",
            "resources/codec-lab-effects.json",
        ],
        "render_contract": {
            "input": "assets/test-video.mp4",
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS,
            "reference_codec_processing_fps": {
                "av2": 6,
                "vvc": 12,
            },
            "variants_per_algorithm": 3,
            "web_codec": "H.264 yuv420p",
            "thumbnail_codec": "WebP",
        },
        "algorithm_count": len(algorithms),
        "video_count": len(algorithms) * 3,
        "family_counts": {
            family: sum(row["family"] == family for row in algorithms)
            for family in FAMILY_INFO
        },
        "algorithms": algorithms,
    }
    return result


def run_command(command: list[str], log_path: Path, timeout: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        log.write(f"\nreturn_code={completed.returncode}\n")
        log.write(f"elapsed_seconds={time.monotonic() - started:.3f}\n")
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with {completed.returncode}; see {log_path}"
        )


def prepare_source(input_path: Path, output_root: Path, ffmpeg: str) -> tuple[Path, Path]:
    work = output_root / "work"
    work.mkdir(parents=True, exist_ok=True)
    normalized = work / "source-480x270-24fps.mp4"
    donor = work / "donor-480x270-24fps.mp4"
    source_digest = sha256(input_path)
    marker = work / "source.json"
    expected = {
        "input_sha256": source_digest,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
    }
    if (
        normalized.is_file()
        and donor.is_file()
        and marker.is_file()
        and read_json(marker) == expected
    ):
        return normalized, donor
    for path in (normalized, donor):
        path.unlink(missing_ok=True)
    run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "16",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(normalized),
        ],
        work / "normalize.log",
        180,
    )
    run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(normalized),
            "-vf",
            "hflip,hue=h=145:s=1.35,reverse",
            "-af",
            "areverse",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(donor),
        ],
        work / "donor.log",
        180,
    )
    marker.write_text(
        json.dumps(expected, indent=2) + "\n", encoding="utf-8"
    )
    return normalized, donor


def resolve_dependencies(catalog: dict[str, Any]) -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required")
    vvenc_candidates = (
        ROOT / ".cache" / "vvenc-v1.14.0" / "bin" / "release-static" / "vvencapp",
        ROOT
        / ".cache"
        / "vvenc-v1.14.0"
        / "build"
        / "bin"
        / "release-static"
        / "vvencapp",
        ROOT / ".cache" / "vvenc-v1.14.0" / "build" / "bin" / "vvencapp",
    )
    vvencapp = next(
        (candidate for candidate in vvenc_candidates if candidate.is_file()),
        vvenc_candidates[0],
    )
    dependencies = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ffedit": str(ROOT / ".cache" / "ffglitch" / "0.10.2" / "bin" / "ffedit"),
        "avmenc": str(ROOT / ".cache" / "avm-v1.0.0" / "build" / "avmenc"),
        "avmdec": str(ROOT / ".cache" / "avm-v1.0.0" / "build" / "avmdec"),
        "vvencapp": str(vvencapp),
    }
    required = {"ffmpeg", "ffprobe"}
    families = {row["family"] for row in catalog["algorithms"]}
    if "native_syntax" in families:
        required.add("ffedit")
    if any(
        row["family"] == "generation" and row["codec"] == "av2"
        for row in catalog["algorithms"]
    ):
        required.update(("avmenc", "avmdec"))
    if any(
        row["family"] == "generation" and row["codec"] == "vvc"
        for row in catalog["algorithms"]
    ):
        required.add("vvencapp")
    missing = [
        name for name in required if not Path(dependencies[name]).is_file()
    ]
    if missing:
        raise RuntimeError(
            "missing gallery dependencies: "
            + ", ".join(missing)
            + ". Run the repository's pinned dependency builders first."
        )
    return dependencies


def processor_command(
    task: RenderTask,
    source: Path,
    donor: Path,
    raw_output: Path,
    report: Path,
    stage_dir: Path,
    dependencies: dict[str, str],
) -> list[str]:
    algorithm = task.algorithm
    parameters = task.preset["parameters"]
    family = algorithm["family"]
    effect = algorithm["effect"]
    codec = algorithm.get("codec")
    python = sys.executable
    # AVM v1.0.0 exposes only its non-realtime Good Quality profile. Sampling
    # the complete five-second source at 2 fps keeps every gallery entry on the
    # official AV2 encode/decode path without turning 15 previews into a
    # multi-hour render. Delivery is normalized back to 24 fps below.
    reference_fps = {"av2": 2, "vvc": 12}
    uses_reference_rate = (
        family == "generation"
        or (family == "codec_lab" and effect == "av2_optical_flow_wound")
    )
    processing_fps = (
        reference_fps[codec]
        if uses_reference_rate and codec in reference_fps
        else FPS
    )
    common_dimensions = [
        "--width",
        str(WIDTH),
        "--height",
        str(HEIGHT),
        "--fps",
        str(processing_fps),
    ]

    if family in {"original", "spatial", "codec_realtime"}:
        command = [
            python,
            str(ROOT / "scripts" / "process_video.py"),
            str(source),
            str(raw_output),
            *common_dimensions,
            "--report",
            str(report),
            "--overwrite",
        ]
        if family == "original":
            command.extend(
                [
                    "--processing-mode",
                    "original_visual",
                    "--preset",
                    effect,
                    "--presets-dir",
                    str(ROOT / "Presets"),
                    "--backend",
                    "metal",
                    "--strength",
                    str(parameters["strength"]),
                    "--filter-bin",
                    str(ROOT / "build" / "glic_original_visual_filter"),
                ]
            )
        elif family == "spatial":
            command.extend(
                [
                    "--processing-mode",
                    "compat_realtime",
                    "--backend",
                    "metal",
                    "--effect-family",
                    effect,
                    "--effect-amount",
                    str(parameters["amount"]),
                    "--effect-scale",
                    str(parameters["scale"]),
                    "--effect-rate",
                    str(parameters["rate"]),
                    "--seed",
                    str(parameters["seed"]),
                    "--filter-bin",
                    str(ROOT / "build" / "glic_realtime_filter"),
                ]
            )
        else:
            command.extend(
                [
                    "--processing-mode",
                    "codec_glitch",
                    "--codec-effect",
                    effect,
                    "--codec-format",
                    str(codec),
                    "--codec-input-pixel-format",
                    "nv12",
                    "--codec-pixel-path",
                    "nv12",
                    "--codec-amount",
                    str(parameters["amount"]),
                    "--codec-rate",
                    str(parameters["rate"]),
                    "--codec-feedback",
                    str(parameters["feedback"]),
                    "--codec-generations",
                    str(max(2, parameters["generations"])),
                    "--seed",
                    str(parameters["seed"]),
                    "--filter-bin",
                    str(ROOT / "build" / "glic_codec_glitch_filter"),
                ]
            )
        return command

    script_for_family = {
        "codec_lab": "process_codec_lab.py",
        "native_syntax": "process_native_syntax_glitch.py",
        "offline_packet": "process_offline_packet_glitch.py",
        "structured": "process_structured_codec_glitch.py",
        "transport": "process_transport_glitch.py",
        "metadata": "process_metadata_glitch.py",
        "generation": "process_multicodec_glitch.py",
    }
    command = [
        python,
        str(ROOT / "scripts" / script_for_family[family]),
        str(source),
        str(raw_output),
        "--effect",
        effect,
        *common_dimensions,
        "--work-dir",
        str(stage_dir),
        "--report",
        str(report),
    ]
    if codec:
        command.extend(["--codec", codec])
    if family in {
        "codec_lab",
        "native_syntax",
        "offline_packet",
        "structured",
        "transport",
        "metadata",
        "generation",
    }:
        command.extend(["--amount", str(parameters["amount"])])
    if family in {"codec_lab", "generation"}:
        command.extend(
            [
                "--rate",
                str(parameters["rate"]),
                "--feedback",
                str(parameters["feedback"]),
            ]
        )
    if family not in {"metadata", "generation"}:
        command.extend(["--seed", str(parameters["seed"])])
    if family in {
        "codec_lab",
        "native_syntax",
        "offline_packet",
        "structured",
        "transport",
        "metadata",
    }:
        maximum_frames = (
            10
            if family == "codec_lab" and effect == "av2_optical_flow_wound"
            else MAX_FRAMES
        )
        processor_timeout = (
            1200
            if family == "codec_lab" and effect == "av2_optical_flow_wound"
            else 300
        )
        command.extend(
            [
                "--max-frames",
                str(maximum_frames),
                "--timeout",
                str(processor_timeout),
            ]
        )
    if family in {
        "codec_lab",
        "native_syntax",
        "offline_packet",
        "structured",
    }:
        command.extend(["--threads", "4"])
    if family in {"codec_lab", "offline_packet", "structured"}:
        command.extend(["--donor", str(donor)])
    if family == "native_syntax":
        command.extend(
            [
                "--source-mode",
                "normalize",
                "--ffedit",
                dependencies["ffedit"],
            ]
        )
    if family == "metadata":
        command.extend(["--segments", str(3 + parameters["generations"])])
    if family == "generation":
        command.extend(
            [
                "--generations",
                str(parameters["generations"]),
                "--threads",
                "4",
                "--avmenc",
                dependencies["avmenc"],
                "--avmdec",
                dependencies["avmdec"],
                "--vvencapp",
                dependencies["vvencapp"],
            ]
        )
        if codec in {"av1", "av2", "vp9", "vvc", "theora", "dirac"}:
            maximum_frames = (
                10
                if codec == "av2"
                else 60
                if codec == "vvc"
                else MAX_FRAMES
            )
            command.extend(["--max-frames", str(maximum_frames)])
    return command


def probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,pix_fmt,r_frame_rate,nb_read_frames,duration",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def web_transcode(
    source: Path,
    raw: Path,
    destination: Path,
    ffmpeg: str,
    log: Path,
    wet_mix: float,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    temporary.unlink(missing_ok=True)
    wet = clamp(float(wet_mix))
    dry = round(1.0 - wet, 3)
    filters = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS},"
        "tpad=stop_mode=clone:stop_duration=5,trim=duration=5,"
        "setpts=PTS-STARTPTS[src];"
        f"[1:v]scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS},"
        "tpad=stop_mode=clone:stop_duration=5,trim=duration=5,"
        "setpts=PTS-STARTPTS[effect];"
        f"[src][effect]blend=all_expr='A*{dry}+B*{wet}':shortest=1,"
        "format=yuv420p[out]"
    )
    run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-i",
            str(raw),
            "-map",
            "[out]",
            "-an",
            "-filter_complex",
            filters,
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "31",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        log,
        180,
    )
    os.replace(temporary, destination)


def entropy_u8(gray: np.ndarray) -> float:
    histogram = cv2.calcHist([gray], [0], None, [64], [0, 256]).ravel()
    probability = histogram / max(1.0, float(histogram.sum()))
    probability = probability[probability > 0]
    return float(-(probability * np.log2(probability)).sum())


def analyze_video(
    source: Path, rendered: Path, thumbnail: Path
) -> dict[str, Any]:
    source_capture = cv2.VideoCapture(str(source))
    render_capture = cv2.VideoCapture(str(rendered))
    if not source_capture.isOpened() or not render_capture.isOpened():
        raise RuntimeError("OpenCV could not open source or rendered video")

    maes: list[float] = []
    changed: list[float] = []
    luminance: list[float] = []
    entropies: list[float] = []
    motion: list[float] = []
    frozen = 0
    frames = 0
    previous: np.ndarray | None = None
    best_score = -math.inf
    best_frame: np.ndarray | None = None
    while True:
        source_ok, source_frame = source_capture.read()
        render_ok, render_frame = render_capture.read()
        if not source_ok or not render_ok:
            break
        if render_frame.shape[:2] != source_frame.shape[:2]:
            source_frame = cv2.resize(
                source_frame,
                (render_frame.shape[1], render_frame.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
        delta = cv2.absdiff(source_frame, render_frame)
        mae = float(np.mean(delta))
        changed_ratio = float(np.mean(np.max(delta, axis=2) >= 12))
        gray = cv2.cvtColor(render_frame, cv2.COLOR_BGR2GRAY)
        frame_entropy = entropy_u8(gray)
        frame_motion = (
            float(np.mean(cv2.absdiff(previous, render_frame)))
            if previous is not None
            else 0.0
        )
        if previous is not None and frame_motion < 0.35:
            frozen += 1
        score = mae * 1.4 + changed_ratio * 40.0 + frame_entropy * 2.0
        if score > best_score and float(np.mean(gray < 5)) < 0.92:
            best_score = score
            best_frame = render_frame.copy()
        maes.append(mae)
        changed.append(changed_ratio)
        luminance.append(float(np.mean(gray)))
        entropies.append(frame_entropy)
        motion.append(frame_motion)
        previous = render_frame
        frames += 1
    source_capture.release()
    render_capture.release()
    if frames < 2 or best_frame is None:
        raise RuntimeError(f"rendered video has too few decodable frames: {frames}")
    thumbnail.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(
        str(thumbnail),
        best_frame,
        [cv2.IMWRITE_WEBP_QUALITY, 82],
    ):
        raise RuntimeError(f"could not write thumbnail: {thumbnail}")
    freeze_ratio = frozen / max(1, frames - 1)
    mean_mae = float(np.mean(maes))
    mean_changed = float(np.mean(changed))
    mean_luma = float(np.mean(luminance))
    status = "PASS"
    warnings: list[str] = []
    if mean_mae < 1.0 or mean_changed < 0.015:
        status = "WARN"
        warnings.append("weak_source_difference")
    if freeze_ratio > 0.96:
        status = "WARN"
        warnings.append("mostly_frozen")
    if mean_luma < 4.0 or mean_luma > 251.0:
        status = "WARN"
        warnings.append("extreme_exposure")
    return {
        "status": status,
        "warnings": warnings,
        "decoded_frames": frames,
        "mean_absolute_difference": round(mean_mae, 4),
        "changed_pixel_ratio": round(mean_changed, 6),
        "mean_luminance": round(mean_luma, 4),
        "mean_luma_entropy": round(float(np.mean(entropies)), 4),
        "mean_frame_delta": round(float(np.mean(motion[1:])), 4),
        "frozen_pair_ratio": round(freeze_ratio, 6),
        "thumbnail_selection": "maximum_difference_complexity_score",
    }


def task_fingerprint(
    task: RenderTask, input_digest: str, revision: str
) -> str:
    payload = {
        "algorithm": task.algorithm,
        "preset": task.preset,
        "input_sha256": input_digest,
        "revision": revision,
        "render_contract": [WIDTH, HEIGHT, FPS, MAX_FRAMES],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def render_task(
    task: RenderTask,
    *,
    source: Path,
    donor: Path,
    source_digest: str,
    revision: str,
    output_root: Path,
    dependencies: dict[str, str],
    timeout: int,
    resume: bool,
) -> dict[str, Any]:
    task_dir = output_root / "work" / "tasks" / task.slug
    stage_dir = task_dir / "stages"
    report = task_dir / "processor-report.json"
    raw = task_dir / "raw.mp4"
    state_path = task_dir / "state.json"
    video = output_root / "site" / "media" / f"{task.slug}.mp4"
    thumbnail = output_root / "site" / "thumbs" / f"{task.slug}.webp"
    fingerprint = task_fingerprint(task, source_digest, revision)
    if resume and state_path.is_file() and video.is_file() and thumbnail.is_file():
        state = read_json(state_path)
        if state.get("fingerprint") == fingerprint and state.get("status") in {
            "PASS",
            "WARN",
        }:
            return state

    if task_dir.exists():
        shutil.rmtree(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    video.unlink(missing_ok=True)
    thumbnail.unlink(missing_ok=True)
    started = time.monotonic()
    state: dict[str, Any] = {
        "schema": "glic-gallery-render-state-v1",
        "fingerprint": fingerprint,
        "algorithm_id": task.algorithm["id"],
        "preset_id": task.preset["id"],
        "status": "FAIL",
    }
    try:
        attempts = [task.preset]
        if task.algorithm["family"] in {
            "offline_packet",
            "structured",
            "transport",
        }:
            for retry in range(1, 5):
                retry_preset = json.loads(json.dumps(task.preset))
                retry_parameters = retry_preset["parameters"]
                retry_parameters["amount"] = clamp(
                    float(task.preset["parameters"]["amount"])
                    * (0.78 ** retry)
                )
                retry_parameters["seed"] = stable_int(
                    f"{task.key}:decode-safe-retry:{retry}"
                )
                retry_preset["rationale"] = (
                    f"{task.preset['rationale']}_decode_safe_retry_{retry}"
                )
                attempts.append(retry_preset)
        processor_errors: list[str] = []
        effective_preset = task.preset
        for attempt_index, attempt_preset in enumerate(attempts):
            raw.unlink(missing_ok=True)
            report.unlink(missing_ok=True)
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            attempt_task = RenderTask(task.algorithm, attempt_preset)
            command = processor_command(
                attempt_task,
                source,
                donor,
                raw,
                report,
                stage_dir,
                dependencies,
            )
            log_name = (
                "processor.log"
                if attempt_index == 0
                else f"processor-retry-{attempt_index}.log"
            )
            try:
                run_command(command, task_dir / log_name, timeout)
                effective_preset = attempt_preset
                break
            except Exception as exc:
                processor_errors.append(str(exc))
        else:
            raise RuntimeError("; ".join(processor_errors))
        if not raw.is_file():
            raise RuntimeError("processor produced no preview video")
        web_transcode(
            source,
            raw,
            video,
            dependencies["ffmpeg"],
            task_dir / "web-transcode.log",
            float(effective_preset["parameters"]["wet_mix"]),
        )
        probe = probe_video(video, dependencies["ffprobe"])
        metrics = analyze_video(source, video, thumbnail)
        stream = probe["streams"][0]
        if (
            stream.get("codec_name") != "h264"
            or int(stream.get("width", 0)) != WIDTH
            or int(stream.get("height", 0)) != HEIGHT
            or stream.get("pix_fmt") != "yuv420p"
        ):
            raise RuntimeError(f"web output contract mismatch: {stream}")
        state.update(
            {
                "status": metrics["status"],
                "warnings": metrics["warnings"],
                "attempt_count": attempts.index(effective_preset) + 1,
                "requested_parameters": task.preset["parameters"],
                "effective_parameters": effective_preset["parameters"],
                "effective_rationale": effective_preset["rationale"],
                "video": f"media/{video.name}",
                "thumbnail": f"thumbs/{thumbnail.name}",
                "video_bytes": video.stat().st_size,
                "video_sha256": sha256(video),
                "thumbnail_bytes": thumbnail.stat().st_size,
                "probe": probe,
                "metrics": metrics,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    except Exception as exc:
        state.update(
            {
                "error": str(exc),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state


def rebuild_web_task(
    task: RenderTask,
    *,
    source: Path,
    source_digest: str,
    revision: str,
    output_root: Path,
    dependencies: dict[str, str],
) -> dict[str, Any]:
    task_dir = output_root / "work" / "tasks" / task.slug
    state_path = task_dir / "state.json"
    raw = task_dir / "raw.mp4"
    video = output_root / "site" / "media" / f"{task.slug}.mp4"
    thumbnail = output_root / "site" / "thumbs" / f"{task.slug}.webp"
    state: dict[str, Any] = (
        read_json(state_path)
        if state_path.is_file()
        else {
            "schema": "glic-gallery-render-state-v1",
            "algorithm_id": task.algorithm["id"],
            "preset_id": task.preset["id"],
        }
    )
    started = time.monotonic()
    try:
        if not raw.is_file():
            raise RuntimeError(f"raw processor output is missing: {raw}")
        requested = dict(
            state.get("requested_parameters", task.preset["parameters"])
        )
        effective = dict(
            state.get("effective_parameters", task.preset["parameters"])
        )
        requested["wet_mix"] = task.preset["parameters"]["wet_mix"]
        effective["wet_mix"] = task.preset["parameters"]["wet_mix"]
        web_transcode(
            source,
            raw,
            video,
            dependencies["ffmpeg"],
            task_dir / "web-rebuild.log",
            float(effective["wet_mix"]),
        )
        probe = probe_video(video, dependencies["ffprobe"])
        metrics = analyze_video(source, video, thumbnail)
        stream = probe["streams"][0]
        if (
            stream.get("codec_name") != "h264"
            or int(stream.get("width", 0)) != WIDTH
            or int(stream.get("height", 0)) != HEIGHT
            or stream.get("pix_fmt") != "yuv420p"
        ):
            raise RuntimeError(f"web output contract mismatch: {stream}")
        state.update(
            {
                "fingerprint": task_fingerprint(
                    task, source_digest, revision
                ),
                "status": metrics["status"],
                "warnings": metrics["warnings"],
                "requested_parameters": requested,
                "effective_parameters": effective,
                "delivery_wet_mix": effective["wet_mix"],
                "video": f"media/{video.name}",
                "thumbnail": f"thumbs/{thumbnail.name}",
                "video_bytes": video.stat().st_size,
                "video_sha256": sha256(video),
                "thumbnail_bytes": thumbnail.stat().st_size,
                "probe": probe,
                "metrics": metrics,
                "delivery_rebuild_seconds": round(
                    time.monotonic() - started, 3
                ),
            }
        )
        state.pop("error", None)
    except Exception as exc:
        state.update(
            {
                "status": "FAIL",
                "error": str(exc),
                "delivery_rebuild_seconds": round(
                    time.monotonic() - started, 3
                ),
            }
        )
    task_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return state


def git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def public_manifest(
    catalog: dict[str, Any],
    states: list[dict[str, Any]],
    input_digest: str,
    revision: str,
) -> dict[str, Any]:
    by_key = {
        (state["algorithm_id"], state["preset_id"]): state for state in states
    }
    algorithms = []
    for algorithm in catalog["algorithms"]:
        row = {key: value for key, value in algorithm.items() if key != "presets"}
        variants = []
        for preset in algorithm["presets"]:
            state = by_key.get((algorithm["id"], preset["id"]), {})
            variants.append(
                {
                    **preset,
                    "parameters": state.get(
                        "effective_parameters", preset["parameters"]
                    ),
                    "requested_parameters": state.get(
                        "requested_parameters", preset["parameters"]
                    ),
                    "render_attempt_count": state.get("attempt_count", 0),
                    "status": state.get("status", "MISSING"),
                    "warnings": state.get("warnings", []),
                    "video": state.get("video"),
                    "thumbnail": state.get("thumbnail"),
                    "video_bytes": state.get("video_bytes"),
                    "video_sha256": state.get("video_sha256"),
                    "metrics": state.get("metrics"),
                    "error": state.get("error"),
                }
            )
        row["variants"] = variants
        algorithms.append(row)
    statuses = [
        variant["status"]
        for algorithm in algorithms
        for variant in algorithm["variants"]
    ]
    return {
        "schema": "glic-glitch-algorithm-gallery-v1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "path": "assets/test-video.mp4",
            "sha256": input_digest,
            "duration_seconds": 5.0,
            "source_resolution": "1920x1080",
        },
        "glic_metal_revision": revision,
        "algorithm_count": len(algorithms),
        "expected_video_count": int(
            catalog.get("video_count", len(algorithms) * 3)
        ),
        "rendered_video_count": sum(status in {"PASS", "WARN"} for status in statuses),
        "status_counts": {
            status: statuses.count(status)
            for status in ("PASS", "WARN", "FAIL", "MISSING")
        },
        "family_counts": catalog["family_counts"],
        "algorithms": algorithms,
    }


def format_parameters(parameters: dict[str, Any]) -> str:
    preferred = (
        "strength",
        "wet_mix",
        "amount",
        "rate",
        "feedback",
        "scale",
        "generations",
        "seed",
    )
    return " · ".join(
        f"{key}={parameters[key]}" for key in preferred if key in parameters
    )


def add_variant_diversity(
    manifest: dict[str, Any], site: Path
) -> None:
    distinct = 0
    weak = 0
    missing = 0
    for algorithm in manifest["algorithms"]:
        images: list[tuple[str, np.ndarray]] = []
        for variant in algorithm["variants"]:
            relative = variant.get("thumbnail")
            if not relative:
                continue
            image = cv2.imread(str(site / relative), cv2.IMREAD_COLOR)
            if image is not None:
                images.append((variant["id"], image.astype(np.float32)))
        if len(images) != len(VARIANT_NAMES):
            algorithm["variant_diversity"] = {
                "status": "MISSING",
                "minimum_pair_mae": None,
                "mean_pair_mae": None,
                "pairs": [],
            }
            missing += 1
            continue
        pairs = [
            {
                "left": left_id,
                "right": right_id,
                "poster_mae": round(
                    float(np.mean(np.abs(left_image - right_image))), 4
                ),
            }
            for (left_id, left_image), (right_id, right_image) in combinations(
                images, 2
            )
        ]
        distances = [pair["poster_mae"] for pair in pairs]
        minimum = min(distances)
        status = "DISTINCT" if minimum >= 1.0 else "WEAK"
        algorithm["variant_diversity"] = {
            "status": status,
            "minimum_pair_mae": round(minimum, 4),
            "mean_pair_mae": round(float(np.mean(distances)), 4),
            "pairs": pairs,
        }
        if status == "DISTINCT":
            distinct += 1
        else:
            weak += 1
    manifest["variant_diversity_counts"] = {
        "DISTINCT": distinct,
        "WEAK": weak,
        "MISSING": missing,
    }


def render_site(manifest: dict[str, Any], site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    add_variant_diversity(manifest, site)
    (site / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    project = {
        "title": "GLIC Metal Glitch Algorithm Gallery",
        "description": (
            f"{manifest['algorithm_count']}種類のグリッチアルゴリズムを、"
            "各3つのパラメータで実動画比較する技術ギャラリー。"
        ),
        "title_en": "GLIC Metal Glitch Algorithm Gallery",
        "description_en": (
            f"A technical gallery comparing {manifest['algorithm_count']} "
            "glitch algorithms with three parameter presets each"
        ),
        "slug": "glic-metal-gallery",
        "visibility": "public",
        "tags": ["public", "Generative", "Metal", "Video", "Codec", "Glitch"],
        "year": "2026",
    }
    (site / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    cards = []
    translations: dict[str, Any] = {
        "gallery-kicker": "GLIC METAL / COMPLETE ALGORITHM INDEX",
        "gallery-title": "Glitch Algorithm Gallery",
        "back-projects": "← PROJECTS",
        "gallery-lead": (
            "Every executable glitch algorithm is rendered from the same "
            "five-second source with three deliberately different parameter presets."
        ),
        "stat-algorithms": "ALGORITHMS",
        "stat-videos": "VIDEOS",
        "stat-qa": "TECHNICAL QA",
        "filter-all": "ALL FAMILIES",
        "search-label": "SEARCH ALGORITHMS",
        "search-placeholder": "effect, codec, implementation…",
        "realtime": "REALTIME",
        "offline": "OFFLINE",
        "implementation": "IMPLEMENTATION",
        "variant-diversity": "VARIANT DIVERSITY",
        "parameters": "PARAMETERS",
        "qa": "TECHNICAL QA",
        "source-note": (
            "Source: one 5-second 1920×1080 H.264 clip. Gallery delivery: "
            "480×270, 24 fps, H.264 yuv420p, muted, WebP poster."
        ),
        "play-hint": "Hover or tap to play",
        "__TEXT__": {
            "effect、codec、implementationを検索": (
                "Search effect, codec, or implementation"
            )
        },
    }
    for family, (label_en, label_ja) in FAMILY_INFO.items():
        translations[f"family-{family}"] = label_en

    for algorithm_index, algorithm in enumerate(manifest["algorithms"]):
        algorithm_key = f"algorithm-{algorithm_index}"
        translations[f"{algorithm_key}-family"] = algorithm["family_label_en"]
        variants_html = []
        searchable = " ".join(
            str(value)
            for value in (
                algorithm["display_name"],
                algorithm["effect"],
                algorithm.get("codec") or "",
                algorithm["implementation_level"],
                algorithm["family"],
            )
        ).lower()
        for variant_index, variant in enumerate(algorithm["variants"]):
            preset_key = f"{algorithm_key}-preset-{variant_index}"
            translations[f"{preset_key}-label"] = variant["label_en"]
            translations[f"{preset_key}-description"] = variant["description_en"]
            status = variant["status"]
            playable = bool(variant.get("video") and variant.get("thumbnail"))
            if playable:
                media = html.escape(variant["video"])
                thumbnail = html.escape(variant["thumbnail"])
                video_markup = (
                    f'<video muted loop playsinline preload="none" '
                    f'poster="{thumbnail}" data-src="{media}" '
                    f'aria-label="{html.escape(algorithm["display_name"])} '
                    f'{html.escape(variant["label_en"])}"></video>'
                )
            else:
                video_markup = (
                    '<div class="media-error">RENDER FAILED<br>'
                    + html.escape(str(variant.get("error") or "missing output"))
                    + "</div>"
                )
            metrics = variant.get("metrics") or {}
            qa_text = (
                f"MAE {metrics.get('mean_absolute_difference', '—')} / "
                f"changed {metrics.get('changed_pixel_ratio', '—')} / "
                f"motion {metrics.get('mean_frame_delta', '—')}"
            )
            variants_html.append(
                f"""
                <article class="variant" data-status="{html.escape(status)}">
                  <div class="media">{video_markup}<span class="play-hint" data-i18n="play-hint">再生: hover / tap</span></div>
                  <div class="variant-copy">
                    <div class="variant-head">
                      <h3 data-i18n="{preset_key}-label">{html.escape(variant["label_ja"])}</h3>
                      <span class="status status-{status.lower()}">{html.escape(status)}</span>
                    </div>
                    <p data-i18n="{preset_key}-description">{html.escape(variant["description_ja"])}</p>
                    <dl>
                      <dt data-i18n="parameters">PARAMETERS</dt>
                      <dd>{html.escape(format_parameters(variant["parameters"]))}</dd>
                      <dt data-i18n="qa">TECHNICAL QA</dt>
                      <dd>{html.escape(qa_text)}</dd>
                    </dl>
                  </div>
                </article>
                """
            )
        realtime_key = "realtime" if algorithm["realtime_certified"] else "offline"
        codec_badge = (
            f'<span class="badge codec">{html.escape(str(algorithm["codec"]).upper())}</span>'
            if algorithm.get("codec")
            else ""
        )
        diversity = algorithm["variant_diversity"]
        diversity_value = (
            "—"
            if diversity["minimum_pair_mae"] is None
            else f"{diversity['minimum_pair_mae']:.2f}"
        )
        cards.append(
            f"""
            <section class="algorithm" data-family="{html.escape(algorithm["family"])}" data-search="{html.escape(searchable)}">
              <header class="algorithm-head">
                <div>
                  <p class="family" data-i18n="{algorithm_key}-family">{html.escape(algorithm["family_label_ja"])}</p>
                  <h2>{html.escape(algorithm["display_name"])}</h2>
                </div>
                <div class="badges">{codec_badge}<span class="badge" data-i18n="{realtime_key}">{'REALTIME' if realtime_key == 'realtime' else 'OFFLINE'}</span></div>
              </header>
              <div class="implementation"><span data-i18n="implementation">IMPLEMENTATION</span> / {html.escape(algorithm["implementation_level"])} · <span data-i18n="variant-diversity">VARIANT DIVERSITY</span> / min Δ {diversity_value} ({html.escape(diversity["status"])})</div>
              <div class="variants">{''.join(variants_html)}</div>
            </section>
            """
        )

    family_options = [
        '<option value="all" data-i18n="filter-all">全ファミリー</option>'
    ]
    for family in FAMILY_INFO:
        family_options.append(
            f'<option value="{family}" data-i18n="family-{family}">'
            f"{html.escape(FAMILY_INFO[family][1])}</option>"
        )
    status = manifest["status_counts"]
    document = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{html.escape(project["description"])}">
  <meta property="og:title" content="GLIC Metal Glitch Algorithm Gallery">
  <meta property="og:description" content="{html.escape(project["description_en"])}">
  <title>GLIC Metal Glitch Algorithm Gallery</title>
  <script src="../scripts/i18n-head.js"></script>
  <style>
    :root{{--bg:#050505;--panel:#0b0b0b;--line:#242424;--text:#f1f1ed;--muted:#8d8d86;--accent:#ff4d2a;--ok:#9bf59b;--warn:#ffd166}}
    *{{box-sizing:border-box}} html{{background:var(--bg);color:var(--text);font-family:Menlo,Monaco,Consolas,"Liberation Mono",monospace}}
    body{{margin:0;background:radial-gradient(circle at 78% -20%,#35110a 0,transparent 33rem),var(--bg)}}
    a{{color:inherit}} .shell{{width:min(1680px,100%);margin:auto;padding:28px clamp(16px,3vw,54px) 96px}}
    .topbar{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:18px}}
    .topbar-left{{display:flex;align-items:center;gap:22px}} .brand{{font-size:11px;letter-spacing:.18em;color:var(--muted)}}
    #back-to-projects{{font-size:10px;letter-spacing:.12em;text-decoration:none;color:#b5b5ad}} #back-to-projects:hover{{color:var(--accent)}}
    [data-i18n-toggle-slot]{{min-height:28px}}
    .hero{{padding:clamp(70px,11vw,180px) 0 74px}} .kicker,.family{{font-size:10px;letter-spacing:.18em;color:var(--accent);text-transform:uppercase}}
    h1{{font:500 clamp(48px,9vw,138px)/.86 Helvetica,Arial,sans-serif;letter-spacing:-.07em;max-width:1200px;margin:22px 0 30px}}
    .lead{{max-width:820px;color:#c5c5bd;font:400 clamp(16px,2vw,25px)/1.48 Helvetica,Arial,sans-serif}}
    .stats{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--line);margin-top:56px}}
    .stat{{padding:24px;border-right:1px solid var(--line)}} .stat:last-child{{border:0}} .stat strong{{display:block;font:500 clamp(30px,5vw,66px)/1 Helvetica,Arial,sans-serif}}
    .stat span{{font-size:10px;color:var(--muted);letter-spacing:.12em}}
    .toolbar{{position:sticky;top:0;z-index:20;display:grid;grid-template-columns:1fr 2fr;gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:42px;box-shadow:0 20px 35px #000a}}
    select,input{{width:100%;border:0;background:#080808;color:var(--text);padding:18px;font:12px Menlo,monospace;outline:none}}
    .algorithm{{border-top:1px solid var(--line);padding:30px 0 68px}} .algorithm[hidden]{{display:none}}
    .algorithm-head{{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:14px}}
    .algorithm h2{{margin:5px 0 0;font:500 clamp(28px,4vw,58px)/1 Helvetica,Arial,sans-serif;letter-spacing:-.035em;text-transform:capitalize}}
    .badges{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}} .badge,.status{{border:1px solid var(--line);padding:7px 9px;font-size:9px;letter-spacing:.08em;color:var(--muted)}}
    .codec{{color:#fff;border-color:#595959}} .implementation{{font-size:9px;color:#666;overflow-wrap:anywhere;margin-bottom:20px}}
    .variants{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
    .variant{{background:var(--panel);border:1px solid var(--line);min-width:0}} .media{{aspect-ratio:16/9;background:#101010;position:relative;overflow:hidden}}
    video{{width:100%;height:100%;object-fit:cover;display:block}} .play-hint{{position:absolute;bottom:8px;left:8px;background:#000b;padding:6px;font-size:8px;color:#aaa;pointer-events:none}}
    .media-error{{padding:20px;color:#ff8973;font-size:10px;overflow-wrap:anywhere}} .variant-copy{{padding:18px}}
    .variant-head{{display:flex;align-items:start;justify-content:space-between;gap:8px}} .variant h3{{font-size:11px;line-height:1.3;margin:0;letter-spacing:.08em}}
    .variant p{{min-height:44px;font:12px/1.5 Helvetica,Arial,sans-serif;color:#aaa}} .status-pass{{color:var(--ok)}} .status-warn{{color:var(--warn)}} .status-fail{{color:#ff6b6b}}
    dl{{margin:18px 0 0;border-top:1px solid var(--line);padding-top:12px}} dt{{font-size:8px;color:#666;margin-top:9px}} dd{{font-size:9px;color:#aaa;margin:4px 0;overflow-wrap:anywhere;line-height:1.5}}
    .foot{{border-top:1px solid var(--line);padding-top:24px;color:#777;font-size:10px;line-height:1.6}}
    @media(max-width:900px){{.variants{{grid-template-columns:1fr}}.toolbar{{grid-template-columns:1fr}}.algorithm-head{{align-items:start;flex-direction:column}}.badges{{justify-content:start}}}}
    @media(max-width:560px){{.shell{{padding-inline:14px}}.stats{{grid-template-columns:1fr}}.stat{{border-right:0;border-bottom:1px solid var(--line)}}.hero{{padding-top:72px}}}}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <div class="topbar-left"><a id="back-to-projects" href="../#generative" data-i18n="back-projects">← PROJECTS</a><span class="brand">GLIC METAL / 2026</span></div>
      <div data-i18n-toggle-slot></div>
    </div>
    <header class="hero">
      <p class="kicker" data-i18n="gallery-kicker">GLIC METAL / 全アルゴリズム索引</p>
      <h1 data-i18n="gallery-title">Glitch Algorithm Gallery</h1>
      <p class="lead" data-i18n="gallery-lead">実行可能な全グリッチアルゴリズムを、同じ5秒の入力動画と、意図的に性格を変えた3つのパラメータで比較します。</p>
      <div class="stats">
        <div class="stat"><strong>{manifest["algorithm_count"]}</strong><span data-i18n="stat-algorithms">ALGORITHMS</span></div>
        <div class="stat"><strong>{manifest["rendered_video_count"]}/{manifest["expected_video_count"]}</strong><span data-i18n="stat-videos">VIDEOS</span></div>
        <div class="stat"><strong>{status.get("PASS", 0)}P / {status.get("WARN", 0)}W</strong><span data-i18n="stat-qa">TECHNICAL QA</span></div>
      </div>
    </header>
    <div class="toolbar">
      <select id="family-filter" aria-label="Filter family">{''.join(family_options)}</select>
      <input id="search" type="search" data-i18n-placeholder="search-placeholder" placeholder="effect、codec、implementationを検索">
    </div>
    <div id="algorithms">{''.join(cards)}</div>
    <footer class="foot">
      <p data-i18n="source-note">入力は5秒・1920×1080のH.264動画。公開動画は480×270・24fps・H.264 yuv420p・無音、posterはWebPです。</p>
      <p><a href="manifest.json">manifest.json</a> · <a href="https://github.com/daitomanabe/glic-metal">github.com/daitomanabe/glic-metal</a></p>
    </footer>
  </main>
  <script>window.__I18N = {json.dumps(translations, ensure_ascii=False)};</script>
  <script src="../scripts/i18n.js"></script>
  <script>
  (() => {{
    const filter = document.querySelector('#family-filter');
    const search = document.querySelector('#search');
    const cards = [...document.querySelectorAll('.algorithm')];
    const apply = () => {{
      const family = filter.value;
      const term = search.value.trim().toLowerCase();
      cards.forEach(card => {{
        card.hidden = !((family === 'all' || card.dataset.family === family) && (!term || card.dataset.search.includes(term)));
      }});
    }};
    filter.addEventListener('change', apply);
    search.addEventListener('input', apply);
    const load = video => {{
      if (!video.src && video.dataset.src) {{ video.src = video.dataset.src; video.load(); }}
    }};
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {{
      if (entry.isIntersecting) load(entry.target);
    }}), {{rootMargin:'500px'}});
    document.querySelectorAll('video').forEach(video => {{
      observer.observe(video);
      video.addEventListener('pointerenter', () => {{ load(video); video.play().catch(()=>{{}}); }});
      video.addEventListener('pointerleave', () => video.pause());
      video.addEventListener('click', () => video.paused ? video.play().catch(()=>{{}}) : video.pause());
    }});
  }})();
  </script>
</body>
</html>
"""
    (site / "index.html").write_text(document, encoding="utf-8")


def select_algorithms(
    catalog: dict[str, Any],
    families: list[str],
    algorithms: list[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    rows = catalog["algorithms"]
    if families:
        rows = [row for row in rows if row["family"] in set(families)]
    if algorithms:
        requested = set(algorithms)
        rows = [
            row
            for row in rows
            if row["id"] in requested or row["effect"] in requested
        ]
    if limit is not None:
        rows = rows[:limit]
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render all GLIC Metal algorithms with three gallery presets."
    )
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--catalog-out", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument("--build-site-only", action="store_true")
    parser.add_argument(
        "--rebuild-web-only",
        action="store_true",
        help=(
            "Reuse existing raw processor outputs and rebuild the five-second "
            "dry/wet web delivery, thumbnails, and technical metrics."
        ),
    )
    parser.add_argument("--family", action="append", choices=tuple(FAMILY_INFO))
    parser.add_argument("--algorithm", action="append")
    parser.add_argument("--variant", action="append", choices=VARIANT_NAMES)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = build_catalog()
    args.catalog_out.parent.mkdir(parents=True, exist_ok=True)
    args.catalog_out.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.catalog_only:
        print(
            json.dumps(
                {
                    "catalog": str(args.catalog_out),
                    "algorithms": catalog["algorithm_count"],
                    "videos": catalog["video_count"],
                    "families": catalog["family_counts"],
                },
                indent=2,
            )
        )
        return 0

    input_path = args.input.expanduser().resolve()
    output_root = args.output_dir.expanduser().resolve()
    if not input_path.is_file():
        raise RuntimeError(f"input video not found: {input_path}")
    algorithms = select_algorithms(
        catalog,
        args.family or [],
        args.algorithm or [],
        args.limit,
    )
    variants = set(args.variant or VARIANT_NAMES)
    tasks = [
        RenderTask(algorithm, preset)
        for algorithm in algorithms
        for preset in algorithm["presets"]
        if preset["id"] in variants
    ]
    dependencies = resolve_dependencies(
        {**catalog, "algorithms": algorithms}
    )
    source, donor = prepare_source(
        input_path, output_root, dependencies["ffmpeg"]
    )
    source_digest = sha256(input_path)
    revision = git_revision()
    states: list[dict[str, Any]] = []

    if args.build_site_only and args.rebuild_web_only:
        raise RuntimeError(
            "--build-site-only and --rebuild-web-only are mutually exclusive"
        )

    if args.build_site_only:
        for task in tasks:
            state_path = output_root / "work" / "tasks" / task.slug / "state.json"
            if state_path.is_file():
                states.append(read_json(state_path))
    elif args.rebuild_web_only:
        print(
            f"Rebuilding {len(tasks)} web videos from existing processor "
            f"outputs with {max(1, args.workers)} worker(s).",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_tasks = {
                executor.submit(
                    rebuild_web_task,
                    task,
                    source=source,
                    source_digest=source_digest,
                    revision=revision,
                    output_root=output_root,
                    dependencies=dependencies,
                ): task
                for task in tasks
            }
            completed = 0
            for future in as_completed(future_tasks):
                task = future_tasks[future]
                state = future.result()
                states.append(state)
                completed += 1
                print(
                    f"[{completed:04d}/{len(tasks):04d}] "
                    f"{state['status']:4s} {task.key} "
                    f"({state.get('delivery_rebuild_seconds', 0):.1f}s)",
                    flush=True,
                )
    else:
        print(
            f"Rendering {len(tasks)} videos from {len(algorithms)} algorithms "
            f"with {max(1, args.workers)} worker(s).",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_tasks = {
                executor.submit(
                    render_task,
                    task,
                    source=source,
                    donor=donor,
                    source_digest=source_digest,
                    revision=revision,
                    output_root=output_root,
                    dependencies=dependencies,
                    timeout=args.timeout,
                    resume=args.resume,
                ): task
                for task in tasks
            }
            completed = 0
            for future in as_completed(future_tasks):
                task = future_tasks[future]
                state = future.result()
                states.append(state)
                completed += 1
                print(
                    f"[{completed:04d}/{len(tasks):04d}] "
                    f"{state['status']:4s} {task.key} "
                    f"({state.get('elapsed_seconds', 0):.1f}s)",
                    flush=True,
                )

    selected_catalog = {
        **catalog,
        "algorithm_count": len(algorithms),
        "video_count": len(tasks),
        "algorithms": algorithms,
        "family_counts": {
            family: sum(row["family"] == family for row in algorithms)
            for family in FAMILY_INFO
        },
    }
    manifest = public_manifest(
        selected_catalog, states, source_digest, revision
    )
    render_site(manifest, output_root / "site")
    print(
        json.dumps(
            {
                "site": str(output_root / "site"),
                "algorithms": manifest["algorithm_count"],
                "expected_videos": manifest["expected_video_count"],
                "rendered_videos": manifest["rendered_video_count"],
                "statuses": manifest["status_counts"],
            },
            indent=2,
        )
    )
    return 0 if manifest["rendered_video_count"] == len(tasks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
