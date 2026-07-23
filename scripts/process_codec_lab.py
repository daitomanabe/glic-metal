#!/usr/bin/env python3
"""Offline codec-syntax reconstruction and analysis-driven glitch lab."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import cv2
import numpy as np

from process_offline_packet_glitch import (
    encoder_options,
    frame_count,
    preview_encoder_options,
    require_tool,
    result_json,
    run_isolated,
    safe_probe,
)


SYNTAX_EFFECTS = (
    "motion_vector_vortex",
    "motion_vector_mirror",
    "motion_vector_quantizer",
    "motion_vector_freeze",
    "residual_sign_flip",
    "residual_band_gate",
    "transform_block_transplant",
    "reference_graph_swap",
    "entropy_state_puncture",
    "loop_filter_oscillator",
    "av1_film_grain_instrument",
    "av2_optical_flow_wound",
)
ANALYSIS_EFFECTS = (
    "semantic_reference_retarget",
    "depth_motion_rift",
    "decoder_fingerprint_ensemble",
    "cross_codec_chain",
    "audio_codec_orchestra",
)
EFFECTS = SYNTAX_EFFECTS + ANALYSIS_EFFECTS
CODECS = ("h264", "hevc", "av1", "vp9", "prores", "av2")

IMPLEMENTATION_LEVEL = {
    **{effect: "decoded_reconstruction_proxy" for effect in SYNTAX_EFFECTS},
    "av1_film_grain_instrument": "av1_codec_cycle_plus_grain_reconstruction",
    "av2_optical_flow_wound": "official_avm_cycle_plus_flow_reconstruction",
    "semantic_reference_retarget": "opencv_motion_semantic_fallback",
    "depth_motion_rift": "deterministic_monocular_depth_proxy",
    "decoder_fingerprint_ensemble": "real_multi_decoder_ensemble",
    "cross_codec_chain": "real_cross_codec_generation_chain",
    "audio_codec_orchestra": "decoded_audio_driven_codec_reconstruction",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clamp_frame(frame: np.ndarray) -> np.ndarray:
    return np.clip(frame, 0, 255).astype(np.uint8)


def optical_flow(previous: np.ndarray, current: np.ndarray) -> np.ndarray:
    previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    return cv2.calcOpticalFlowFarneback(
        previous_gray,
        current_gray,
        None,
        0.5,
        3,
        21,
        3,
        5,
        1.2,
        0,
    )


def warp_with_flow(frame: np.ndarray, flow: np.ndarray) -> np.ndarray:
    height, width = frame.shape[:2]
    x, y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )
    return cv2.remap(
        frame,
        x + flow[..., 0],
        y + flow[..., 1],
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )


def mix(first: np.ndarray, second: np.ndarray, amount: float) -> np.ndarray:
    return cv2.addWeighted(first, 1.0 - amount, second, amount, 0.0)


def moving_mask(
    frame: np.ndarray, previous: np.ndarray, amount: float
) -> np.ndarray:
    delta = cv2.absdiff(frame, previous)
    gray = cv2.cvtColor(delta, cv2.COLOR_BGR2GRAY)
    threshold = max(6, round(38 - amount * 28))
    mask = np.where(gray >= threshold, 255, 0).astype(np.uint8)
    kernel = np.ones((9, 9), dtype=np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)


def block_transplant(
    frame: np.ndarray,
    donor: np.ndarray,
    amount: float,
    seed: int,
    frame_index: int,
) -> np.ndarray:
    result = frame.copy()
    height, width = frame.shape[:2]
    rng = np.random.default_rng(seed ^ (frame_index * 0x9E3779B1))
    block = max(8, round(64 - amount * 48))
    count = 2 + round(amount * 10)
    for _ in range(count):
        size = min(block * int(rng.integers(1, 4)), width, height)
        x = int(rng.integers(0, max(1, width - size + 1)))
        y = int(rng.integers(0, max(1, height - size + 1)))
        sx = int(rng.integers(0, max(1, width - size + 1)))
        sy = int(rng.integers(0, max(1, height - size + 1)))
        result[y : y + size, x : x + size] = donor[
            sy : sy + size, sx : sx + size
        ]
    return result


def transform_frame(
    effect: str,
    frame: np.ndarray,
    history: deque[np.ndarray],
    donor: np.ndarray | None,
    frozen_flow: np.ndarray | None,
    index: int,
    amount: float,
    rate: float,
    feedback: float,
    seed: int,
    audio_level: float,
) -> tuple[np.ndarray, np.ndarray | None]:
    if not history:
        return frame, frozen_flow
    previous = history[-1]
    far = history[max(0, len(history) - 1 - min(7, len(history) - 1))]
    strength = float(np.clip(amount * (0.65 + audio_level * 0.7), 0.0, 1.0))

    if effect.startswith("motion_vector_") or effect == "av2_optical_flow_wound":
        flow = optical_flow(previous, frame)
        if effect == "motion_vector_vortex":
            height, width = frame.shape[:2]
            x, y = np.meshgrid(
                np.linspace(-1.0, 1.0, width, dtype=np.float32),
                np.linspace(-1.0, 1.0, height, dtype=np.float32),
            )
            angle = strength * math.pi * np.sqrt(x * x + y * y)
            cosine, sine = np.cos(angle), np.sin(angle)
            fx = flow[..., 0] * cosine - flow[..., 1] * sine
            fy = flow[..., 0] * sine + flow[..., 1] * cosine
            flow = np.dstack((fx - y * 18.0 * strength, fy + x * 18.0 * strength))
        elif effect == "motion_vector_mirror":
            flow[..., 0] *= -1.0
            if index % max(2, round(8 - rate * 6)) == 0:
                flow[..., 1] *= -1.0
        elif effect == "motion_vector_quantizer":
            quantum = max(1.0, 2.0 + strength * 14.0)
            flow = np.round(flow / quantum) * quantum
        elif effect == "motion_vector_freeze":
            period = max(2, round(12 - rate * 10))
            if frozen_flow is None or index % period == 0:
                frozen_flow = flow.copy()
            flow = frozen_flow
            mask = moving_mask(frame, previous, strength)
            warped = warp_with_flow(previous, flow)
            selected = np.where(mask[..., None] > 0, warped, frame)
            return mix(frame, selected, 0.45 + strength * 0.5), frozen_flow
        else:
            height, width = frame.shape[:2]
            x, y = np.meshgrid(
                np.arange(width, dtype=np.float32),
                np.arange(height, dtype=np.float32),
            )
            wound = (
                np.sin((x + index * (2.0 + rate * 7.0)) * 0.035) > 0
            ).astype(np.float32)
            flow *= (1.0 - wound[..., None]) - wound[..., None] * (
                0.45 + strength
            )
        warped = warp_with_flow(previous, flow)
        return mix(frame, warped, 0.35 + strength * 0.6), frozen_flow

    if effect == "residual_sign_flip":
        prediction = previous.astype(np.float32)
        residual = frame.astype(np.float32) - prediction
        return clamp_frame(prediction - residual * (0.4 + strength * 1.35)), frozen_flow

    if effect == "residual_band_gate":
        sigma = 1.0 + strength * 8.0
        low = cv2.GaussianBlur(frame, (0, 0), sigma)
        high = frame.astype(np.float32) - low.astype(np.float32)
        if (index // max(1, round(6 - rate * 5))) & 1:
            gated = clamp_frame(128.0 + high * (1.5 + strength * 3.0))
        else:
            gated = low
        return mix(frame, gated, 0.45 + strength * 0.5), frozen_flow

    if effect == "transform_block_transplant":
        return (
            block_transplant(frame, donor if donor is not None else far, strength, seed, index),
            frozen_flow,
        )

    if effect == "reference_graph_swap":
        ages = (1, 3, 2, 6, 4)
        age = ages[index % len(ages)]
        selected = history[max(0, len(history) - 1 - min(age, len(history) - 1))]
        return mix(frame, selected, 0.40 + strength * 0.58), frozen_flow

    if effect == "entropy_state_puncture":
        result = frame.copy()
        rng = np.random.default_rng(seed + index * 17)
        height, width = frame.shape[:2]
        block = max(8, round(48 - strength * 32))
        for _ in range(2 + round(strength * 16)):
            x = int(rng.integers(0, max(1, width - block)))
            y = int(rng.integers(0, max(1, height - block)))
            region = result[y : y + block, x : x + block]
            mask = rng.integers(0, 256, size=(1, 1, 3), dtype=np.uint8)
            result[y : y + block, x : x + block] = cv2.bitwise_xor(
                region, np.broadcast_to(mask, region.shape)
            )
        return result, frozen_flow

    if effect == "loop_filter_oscillator":
        wave = 0.5 + 0.5 * math.sin(index * (0.08 + rate * 0.42))
        blurred = cv2.GaussianBlur(frame, (0, 0), 0.6 + wave * strength * 8.0)
        sharpened = cv2.addWeighted(frame, 1.8 + strength, blurred, -0.8 - strength, 0)
        return mix(blurred, sharpened, wave), frozen_flow

    if effect == "av1_film_grain_instrument":
        rng = np.random.default_rng(seed ^ (index * 0x45D9F3B))
        monochrome = rng.normal(
            0.0, 4.0 + strength * 34.0, size=frame.shape[:2]
        ).astype(np.float32)
        grain = np.repeat(monochrome[..., None], 3, axis=2)
        return clamp_frame(frame.astype(np.float32) + grain), frozen_flow

    if effect == "semantic_reference_retarget":
        mask = moving_mask(frame, previous, strength)
        mask = cv2.GaussianBlur(mask, (0, 0), 5.0 + strength * 8.0)
        selected = mix(previous, far, feedback)
        return np.where(mask[..., None] > 64, selected, frame), frozen_flow

    if effect == "depth_motion_rift":
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        gray = cv2.GaussianBlur(gray, (0, 0), 8.0)
        height, width = gray.shape
        vertical = np.linspace(1.0, 0.0, height, dtype=np.float32)[:, None]
        depth = gray * 0.55 + vertical * 0.45
        x, y = np.meshgrid(
            np.arange(width, dtype=np.float32),
            np.arange(height, dtype=np.float32),
        )
        phase = math.sin(index * (0.05 + rate * 0.25))
        displacement = (depth - 0.5) * phase * strength * width * 0.16
        warped = cv2.remap(
            frame,
            x + displacement,
            y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        return mix(frame, warped, 0.5 + strength * 0.45), frozen_flow

    if effect == "decoder_fingerprint_ensemble":
        small = cv2.resize(
            frame,
            (max(2, frame.shape[1] // 12), max(2, frame.shape[0] // 12)),
            interpolation=cv2.INTER_AREA,
        )
        pixel = cv2.resize(
            small, (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_NEAREST
        )
        jpeg_quality = max(5, round(55 - strength * 48))
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        jpeg = cv2.imdecode(encoded, cv2.IMREAD_COLOR) if ok else frame
        result = frame.copy()
        result[:, ::3] = pixel[:, ::3]
        result[:, 1::3] = jpeg[:, 1::3]
        result[:, 2::3] = far[:, 2::3]
        return result, frozen_flow

    if effect == "cross_codec_chain":
        return block_transplant(frame, far, strength * 0.7, seed, index), frozen_flow

    if effect == "audio_codec_orchestra":
        rng = np.random.default_rng(seed + index)
        result = frame.copy()
        stripe = max(2, round(28 - audio_level * 22))
        offset = round((audio_level - 0.5) * frame.shape[1] * 0.24)
        for y in range((index * stripe) % (stripe * 2), frame.shape[0], stripe * 2):
            result[y : y + stripe] = np.roll(
                result[y : y + stripe], offset, axis=1
            )
        noise = rng.normal(0, audio_level * strength * 24, result.shape)
        return clamp_frame(result.astype(np.float32) + noise), frozen_flow

    return frame, frozen_flow


def audio_envelope(ffmpeg: str, source: Path, frame_count_value: int, fps: int) -> list[float]:
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0?",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        return [
            0.5 + 0.5 * math.sin(index * 0.37) for index in range(frame_count_value)
        ]
    per_frame = max(1, round(8000 / fps))
    levels = []
    for index in range(frame_count_value):
        block = samples[index * per_frame : (index + 1) * per_frame]
        levels.append(float(np.sqrt(np.mean(block * block))) if block.size else 0.0)
    peak = max(levels, default=1.0)
    return [min(1.0, level / max(peak, 1.0e-9)) for level in levels]


def transcode_cycle(
    ffmpeg: str,
    source: Path,
    codec: str,
    fps: int,
    threads: int,
    work: Path,
    timeout: int,
    maximum_file_bytes: int,
) -> tuple[Path, list[dict], Path | None]:
    work.mkdir(parents=True, exist_ok=True)
    encoded = work / f"syntax-{codec}.mkv"
    decoded = work / f"syntax-{codec}-decoded.mkv"
    encode = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-an",
            "-vf",
            "format=yuv420p",
            *encoder_options(codec, fps, threads),
            str(encoded),
        ],
        log=work / f"codec-{codec}-encode.log",
        timeout_seconds=timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if encode.return_code != 0 or not encoded.is_file():
        raise RuntimeError(f"{codec} encode failed; see {encode.log}")
    decode = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(encoded),
            "-an",
            "-c:v",
            "ffv1",
            str(decoded),
        ],
        log=work / f"codec-{codec}-decode.log",
        timeout_seconds=timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    if decode.return_code != 0 or not decoded.is_file():
        raise RuntimeError(f"{codec} decode failed; see {decode.log}")
    return decoded, [result_json(encode), result_json(decode)], encoded


def cross_codec_chain(
    ffmpeg: str,
    source: Path,
    fps: int,
    threads: int,
    work: Path,
    timeout: int,
    maximum_file_bytes: int,
) -> tuple[Path, list[dict], list[Path]]:
    current = source
    processes: list[dict] = []
    bitstreams: list[Path] = []
    for codec in ("av1", "vp9", "hevc", "prores"):
        current, stage_processes, bitstream = transcode_cycle(
            ffmpeg,
            current,
            codec,
            fps,
            threads,
            work / codec,
            timeout,
            maximum_file_bytes,
        )
        processes.extend(stage_processes)
        if bitstream:
            bitstreams.append(bitstream)
    return current, processes, bitstreams


def decoder_ensemble(
    ffmpeg: str,
    source: Path,
    fps: int,
    threads: int,
    work: Path,
    timeout: int,
    maximum_file_bytes: int,
) -> tuple[Path, list[dict], list[Path]]:
    decoded_paths = []
    processes: list[dict] = []
    bitstreams: list[Path] = []
    for codec in ("h264", "hevc", "prores"):
        decoded, stage_processes, bitstream = transcode_cycle(
            ffmpeg,
            source,
            codec,
            fps,
            threads,
            work / codec,
            timeout,
            maximum_file_bytes,
        )
        decoded_paths.append(decoded)
        processes.extend(stage_processes)
        if bitstream:
            bitstreams.append(bitstream)
    ensemble = work / "decoder-ensemble.mkv"
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *sum((["-i", str(path)] for path in decoded_paths), []),
        "-filter_complex",
        (
            "[0:v][1:v]blend=all_mode=grainmerge:all_opacity=0.45[a];"
            "[a][2:v]blend=all_mode=overlay:all_opacity=0.35[v]"
        ),
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "ffv1",
        ensemble,
    ]
    result = run_isolated(
        [str(item) for item in command],
        log=work / "decoder-ensemble.log",
        timeout_seconds=timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    processes.append(result_json(result))
    if result.return_code != 0 or not ensemble.is_file():
        raise RuntimeError(f"decoder ensemble failed; see {result.log}")
    return ensemble, processes, bitstreams


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--effect", choices=EFFECTS, required=True)
    parser.add_argument("--codec", choices=CODECS, default="h264")
    parser.add_argument("--donor", type=Path)
    parser.add_argument("--amount", type=float, default=0.68)
    parser.add_argument("--rate", type=float, default=0.52)
    parser.add_argument("--feedback", type=float, default=0.64)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x474C4943)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max-frames", type=int, default=180)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--maximum-file-mib", type=int, default=4096)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if args.donor:
        args.donor = args.donor.expanduser().resolve()
        if not args.donor.is_file():
            parser.error(f"donor does not exist: {args.donor}")
    if args.width < 2 or args.height < 2 or args.width % 2 or args.height % 2:
        parser.error("--width and --height must be positive even values")
    if not all(0.0 <= value <= 1.0 for value in (args.amount, args.rate, args.feedback)):
        parser.error("--amount, --rate, and --feedback must be in [0, 1]")
    if min(args.fps, args.max_frames, args.threads, args.timeout) < 1:
        parser.error("fps, max-frames, threads, and timeout must be positive")
    if args.effect == "av1_film_grain_instrument":
        args.codec = "av1"
    if args.effect == "av2_optical_flow_wound":
        args.codec = "av2"
    return args


def main() -> int:
    args = parse_args()
    ffmpeg = require_tool(args.ffmpeg)
    ffprobe = require_tool(args.ffprobe)
    work = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else args.output.with_suffix(args.output.suffix + ".codec-lab-stages")
    )
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else args.output.with_suffix(args.output.suffix + ".json")
    )
    work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    maximum_file_bytes = args.maximum_file_mib * 1024 * 1024

    capture = cv2.VideoCapture(str(args.input))
    if not capture.isOpened():
        raise RuntimeError(f"could not decode input: {args.input}")
    donor_capture = cv2.VideoCapture(str(args.donor)) if args.donor else None
    if donor_capture is not None and not donor_capture.isOpened():
        raise RuntimeError(f"could not decode donor: {args.donor}")
    transformed = work / "reconstruction.ffv1.mkv"
    writer = cv2.VideoWriter(
        str(transformed),
        cv2.VideoWriter_fourcc(*"FFV1"),
        float(args.fps),
        (args.width, args.height),
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV FFV1 writer is unavailable")

    envelope = audio_envelope(ffmpeg, args.input, args.max_frames, args.fps)
    history: deque[np.ndarray] = deque(maxlen=12)
    frozen_flow = None
    processed_frames = 0
    started = time.monotonic()
    try:
        while processed_frames < args.max_frames:
            success, frame = capture.read()
            if not success:
                break
            frame = cv2.resize(
                frame, (args.width, args.height), interpolation=cv2.INTER_AREA
            )
            donor_frame = None
            if donor_capture is not None:
                donor_success, donor_frame = donor_capture.read()
                if not donor_success:
                    donor_capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    donor_success, donor_frame = donor_capture.read()
                if donor_success:
                    donor_frame = cv2.resize(
                        donor_frame,
                        (args.width, args.height),
                        interpolation=cv2.INTER_AREA,
                    )
            output, frozen_flow = transform_frame(
                args.effect,
                frame,
                history,
                donor_frame,
                frozen_flow,
                processed_frames,
                args.amount,
                args.rate,
                args.feedback,
                args.seed,
                envelope[processed_frames],
            )
            writer.write(output)
            history.append(frame.copy())
            processed_frames += 1
    finally:
        writer.release()
        capture.release()
        if donor_capture is not None:
            donor_capture.release()
    reconstruction_seconds = time.monotonic() - started
    if processed_frames < 2 or not transformed.is_file():
        raise RuntimeError("fewer than two frames were reconstructed")

    processes: list[dict] = []
    bitstreams: list[Path] = []
    if args.effect == "cross_codec_chain":
        decoded, processes, bitstreams = cross_codec_chain(
            ffmpeg,
            transformed,
            args.fps,
            args.threads,
            work / "cross-codec",
            args.timeout,
            maximum_file_bytes,
        )
    elif args.effect == "decoder_fingerprint_ensemble":
        decoded, processes, bitstreams = decoder_ensemble(
            ffmpeg,
            transformed,
            args.fps,
            args.threads,
            work / "ensemble",
            args.timeout,
            maximum_file_bytes,
        )
    elif args.codec == "av2":
        av2_preview = work / "av2-preview.mp4"
        command = [
            sys.executable,
            str(Path(__file__).with_name("process_multicodec_glitch.py")),
            str(transformed),
            str(av2_preview),
            "--codec",
            "av2",
            "--effect",
            "generation_cascade",
            "--generations",
            "1",
            "--amount",
            str(args.amount),
            "--width",
            str(args.width),
            "--height",
            str(args.height),
            "--fps",
            str(args.fps),
            "--max-frames",
            str(processed_frames),
            "--threads",
            str(args.threads),
            "--work-dir",
            str(work / "av2-cycle"),
        ]
        result = run_isolated(
            command,
            log=work / "av2-cycle.log",
            timeout_seconds=args.timeout,
            maximum_file_bytes=maximum_file_bytes,
        )
        processes.append(result_json(result))
        if result.return_code != 0 or not av2_preview.is_file():
            raise RuntimeError(f"AV2 reference cycle failed; see {result.log}")
        decoded = av2_preview
        bitstreams = sorted((work / "av2-cycle").glob("*.ivf"))
    else:
        decoded, processes, bitstream = transcode_cycle(
            ffmpeg,
            transformed,
            args.codec,
            args.fps,
            args.threads,
            work,
            args.timeout,
            maximum_file_bytes,
        )
        if bitstream:
            bitstreams.append(bitstream)

    preview_options = preview_encoder_options(ffmpeg)
    preview = run_isolated(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(decoded),
            "-i",
            str(args.input),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            *preview_options,
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(args.output),
        ],
        log=work / "preview.log",
        timeout_seconds=args.timeout,
        maximum_file_bytes=maximum_file_bytes,
    )
    processes.append(result_json(preview))
    output_probe = safe_probe(ffprobe, args.output)
    output_frames = frame_count(output_probe)
    qualified = (
        preview.return_code == 0
        and args.output.is_file()
        and output_frames >= 2
    )
    report = {
        "schema": "glic-codec-lab-v1",
        "execution_class": "offline",
        "realtime_certified": False,
        "effect_class": (
            "syntax_reconstruction"
            if args.effect in SYNTAX_EFFECTS
            else "analysis_driven"
        ),
        "effect": args.effect,
        "implementation_level": IMPLEMENTATION_LEVEL[args.effect],
        "codec": args.codec,
        "input": str(args.input),
        "donor": str(args.donor) if args.donor else None,
        "output": str(args.output),
        "controls": {
            "amount": args.amount,
            "rate": args.rate,
            "feedback": args.feedback,
            "seed": args.seed,
        },
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "input_frames_processed": processed_frames,
        "output_frames": output_frames,
        "reconstruction_seconds": round(reconstruction_seconds, 6),
        "qualified_preview": qualified,
        "codec_evidence": [
            {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
                "probe": safe_probe(ffprobe, path),
            }
            for path in bitstreams
            if path.is_file()
        ],
        "processes": processes,
        "output_probe": output_probe,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"effect={args.effect} codec={args.codec} "
        f"frames={output_frames}/{processed_frames} qualified={qualified} "
        f"report={report_path}"
    )
    return 0 if qualified else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
