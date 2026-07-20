#!/usr/bin/env python3
"""Extract deterministic perceptual features from still images.

The positional input syntax matches ``visual-liveliness``::

    image.png
    image.png|1.0|candidate-label

The middle field is retained as ``start_seconds`` for schema compatibility but
does not affect still-image analysis.  All descriptors are fixed-size,
normalised arrays intended for deterministic ranking and perceptual-distance
calculations; this tool deliberately does not assign an aesthetic score.  A
successful ``--json`` report is a bare array of row objects, matching the
external visual-liveliness instrument.  If any input fails, the report is an
empty array and the process exits nonzero so partial batches cannot be ranked.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import cv2
import numpy as np


SCHEMA_VERSION = 1
ANALYSIS_SIZE = (256, 256)
HSV_BINS = (8, 4, 4)
LUMA_GRID = (8, 8)
EDGE_GRID = (8, 8)
COLOR_GRID = (4, 4, 3)


DESCRIPTOR_SCHEMA: dict[str, dict[str, Any]] = {
    "hsv_hist": {
        "shape": list(HSV_BINS),
        "length": int(np.prod(HSV_BINS)),
        "range": [0.0, 1.0],
        "normalization": "sum=1",
        "order": "H-major, then S, then V",
    },
    "luma_grid": {
        "shape": list(LUMA_GRID),
        "length": int(np.prod(LUMA_GRID)),
        "range": [0.0, 1.0],
        "normalization": "luma/255",
        "order": "row-major",
    },
    "edge_grid": {
        "shape": list(EDGE_GRID),
        "length": int(np.prod(EDGE_GRID)),
        "range": [0.0, 1.0],
        "normalization": "Canny edge occupancy per cell",
        "order": "row-major",
    },
    "color_grid": {
        "shape": list(COLOR_GRID),
        "length": int(np.prod(COLOR_GRID)),
        "range": [0.0, 1.0],
        "normalization": "RGB/255",
        "order": "row-major RGB",
    },
}


def _round_scalar(value: float) -> float:
    """Keep JSON compact and stable without discarding ranking resolution."""
    result = round(float(value), 8)
    if not math.isfinite(result):
        raise ValueError("feature computation produced a non-finite scalar")
    return result


def _round_array(values: np.ndarray) -> list[float]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    if not np.isfinite(flat).all():
        raise ValueError("feature computation produced a non-finite descriptor")
    return [round(float(value), 6) for value in flat]


def parse_input_spec(spec: str) -> tuple[Path, float, str]:
    """Parse ``path`` or ``path|start|label`` like visual-liveliness."""
    parts = spec.split("|")
    raw_path = parts[0]
    if not raw_path:
        raise ValueError("input path is empty")
    try:
        start = float(parts[1]) if len(parts) > 1 and parts[1] else 1.0
    except ValueError as error:
        raise ValueError(f"invalid start time: {parts[1]!r}") from error
    if not math.isfinite(start):
        raise ValueError("start time must be finite")
    label = parts[2] if len(parts) > 2 and parts[2] else os.path.basename(raw_path)
    return Path(raw_path).expanduser(), start, label


def _bits_to_hex(bits: np.ndarray) -> str:
    value = 0
    flat = np.asarray(bits, dtype=np.uint8).reshape(-1)
    for bit in flat:
        value = (value << 1) | int(bool(bit))
    return f"{value:0{(flat.size + 3) // 4}x}"


def perceptual_hash(gray: np.ndarray) -> str:
    """Return a conventional 64-bit DCT perceptual hash."""
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    low = cv2.dct(small)[:8, :8]
    threshold = float(np.median(low.reshape(-1)[1:]))
    return _bits_to_hex(low > threshold)


def difference_hash(gray: np.ndarray) -> str:
    """Return a 64-bit horizontal difference hash."""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return _bits_to_hex(small[:, :-1] > small[:, 1:])


def _hsv_histogram(hsv: np.ndarray) -> np.ndarray:
    h = np.minimum((hsv[..., 0].astype(np.int32) * HSV_BINS[0]) // 180, HSV_BINS[0] - 1)
    s = np.minimum((hsv[..., 1].astype(np.int32) * HSV_BINS[1]) // 256, HSV_BINS[1] - 1)
    v = np.minimum((hsv[..., 2].astype(np.int32) * HSV_BINS[2]) // 256, HSV_BINS[2] - 1)
    indices = (h * HSV_BINS[1] + s) * HSV_BINS[2] + v
    histogram = np.bincount(indices.reshape(-1), minlength=int(np.prod(HSV_BINS))).astype(
        np.float64
    )
    histogram /= max(1.0, float(histogram.sum()))
    return histogram


def _grid(channel: np.ndarray, rows: int, columns: int, scale: float) -> np.ndarray:
    reduced = cv2.resize(channel, (columns, rows), interpolation=cv2.INTER_AREA)
    return reduced.astype(np.float64) / scale


def _blockiness(gray: np.ndarray, period: int = 8) -> float:
    """Estimate excess discontinuity on an 8-pixel block lattice.

    The baseline is the mean non-lattice adjacent-pixel difference, so natural
    image detail contributes little while aligned block discontinuities remain.
    """
    values = gray.astype(np.float32)
    vertical = np.abs(np.diff(values, axis=1))
    horizontal = np.abs(np.diff(values, axis=0))

    vertical_boundary = (np.arange(1, values.shape[1]) % period) == 0
    horizontal_boundary = (np.arange(1, values.shape[0]) % period) == 0
    vertical_inside = ~vertical_boundary
    horizontal_inside = ~horizontal_boundary

    boundary_samples = np.concatenate(
        [vertical[:, vertical_boundary].reshape(-1), horizontal[horizontal_boundary, :].reshape(-1)]
    )
    inside_samples = np.concatenate(
        [vertical[:, vertical_inside].reshape(-1), horizontal[horizontal_inside, :].reshape(-1)]
    )
    excess = float(boundary_samples.mean() - inside_samples.mean())
    return max(0.0, excess / 255.0)


def extract_features(image_bgr: np.ndarray) -> dict[str, Any]:
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("expected a three-channel BGR image")
    if image_bgr.size == 0:
        raise ValueError("image is empty")

    working = cv2.resize(image_bgr, ANALYSIS_SIZE, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(working, cv2.COLOR_BGR2HSV)
    rgb = cv2.cvtColor(working, cv2.COLOR_BGR2RGB)

    rgb_float = rgb.astype(np.float32)
    red, green, blue = cv2.split(rgb_float)
    red_green = red - green
    yellow_blue = 0.5 * (red + green) - blue
    colorfulness = (
        math.hypot(float(red_green.std()), float(yellow_blue.std()))
        + 0.3 * math.hypot(float(red_green.mean()), float(yellow_blue.mean()))
    ) / 255.0

    saturation = hsv[..., 1].astype(np.float32) / 255.0
    gray_float = gray.astype(np.float32)
    low_frequency = cv2.GaussianBlur(gray_float, (0, 0), 3.0)
    local_contrast = float(np.sqrt(np.mean(np.square(gray_float - low_frequency)))) / 255.0
    edges = cv2.Canny(gray, 64, 128, L2gradient=True)
    edge_density = float(np.count_nonzero(edges)) / float(edges.size)
    channel_separation = float(
        (np.abs(red - green) + np.abs(green - blue) + np.abs(blue - red)).mean()
    ) / (3.0 * 255.0)

    color_grid = cv2.resize(rgb, (COLOR_GRID[1], COLOR_GRID[0]), interpolation=cv2.INTER_AREA)
    descriptors = {
        "hsv_hist": _round_array(_hsv_histogram(hsv)),
        "luma_grid": _round_array(_grid(gray, LUMA_GRID[0], LUMA_GRID[1], 255.0)),
        "edge_grid": _round_array(_grid(edges, EDGE_GRID[0], EDGE_GRID[1], 255.0)),
        "color_grid": _round_array(color_grid.astype(np.float64) / 255.0),
    }
    return {
        "colorfulness": _round_scalar(colorfulness),
        "saturation_mean": _round_scalar(saturation.mean()),
        "saturation_std": _round_scalar(saturation.std()),
        "local_contrast": _round_scalar(local_contrast),
        "edge_density": _round_scalar(edge_density),
        "blockiness": _round_scalar(_blockiness(gray)),
        "channel_separation": _round_scalar(channel_separation),
        "phash": perceptual_hash(gray),
        "dhash": difference_hash(gray),
        **descriptors,
    }


def analyse_spec(spec: str) -> dict[str, Any]:
    path, start, label = parse_input_spec(spec)
    if not path.is_file():
        raise FileNotFoundError(f"input image does not exist: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode image: {path}")
    height, width = image.shape[:2]
    return {
        "name": label,
        "source": str(path.resolve()),
        "start_seconds": _round_scalar(start),
        "width": int(width),
        "height": int(height),
        **extract_features(image),
    }


def build_document(specs: list[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for spec in specs:
        try:
            rows.append(analyse_spec(spec))
        except Exception as error:  # one bad input invalidates the overall batch
            try:
                _, _, name = parse_input_spec(spec)
            except Exception:
                name = spec
            errors.append(
                {
                    "input": spec,
                    "name": name,
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "glic-metal-perceptual-image-features",
        "status": "error" if errors else "ok",
        "analysis_size": list(ANALYSIS_SIZE),
        "descriptor_schema": DESCRIPTOR_SCHEMA,
        "rows": rows,
        "errors": errors,
        "summary": {
            "requested": len(specs),
            "succeeded": len(rows),
            "failed": len(errors),
        },
    }


def _selftest_image(kind: str) -> np.ndarray:
    if kind == "flat":
        return np.full((256, 256, 3), 128, dtype=np.uint8)
    if kind == "checker":
        yy, xx = np.indices((256, 256))
        cells = ((xx // 8 + yy // 8) % 2).astype(np.uint8)
        return np.where(cells[..., None] != 0, 240, 16).astype(np.uint8).repeat(3, axis=2)
    if kind == "color":
        yy, xx = np.indices((256, 256))
        return np.stack(
            [xx.astype(np.uint8), yy.astype(np.uint8), (255 - xx).astype(np.uint8)], axis=2
        )
    raise ValueError(kind)


def selftest() -> int:
    flat = extract_features(_selftest_image("flat"))
    checker = extract_features(_selftest_image("checker"))
    color = extract_features(_selftest_image("color"))
    checks = {
        "colorfulness_discriminates": color["colorfulness"] > flat["colorfulness"] + 0.1,
        "saturation_discriminates": color["saturation_mean"] > flat["saturation_mean"] + 0.1,
        "contrast_discriminates": checker["local_contrast"] > flat["local_contrast"] + 0.05,
        "edges_discriminate": checker["edge_density"] > flat["edge_density"] + 0.05,
        "blocks_discriminate": checker["blockiness"] > flat["blockiness"] + 0.05,
        "hashes_are_fixed_width": all(
            len(features[key]) == 16
            for features in (flat, checker, color)
            for key in ("phash", "dhash")
        ),
        "histogram_is_normalized": abs(sum(color["hsv_hist"]) - 1.0) < 1e-5,
        "descriptor_lengths_match": all(
            len(color[name]) == schema["length"]
            for name, schema in DESCRIPTOR_SCHEMA.items()
        ),
        "deterministic": color == extract_features(_selftest_image("color")),
    }
    ok = all(checks.values())
    print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
    print(json.dumps({"status": "passed" if ok else "failed", "checks": checks}, indent=2))
    return 0 if ok else 1


def write_json(path: Path, payload: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="image path or path|start|label")
    parser.add_argument("--json", type=Path, metavar="OUT", help="write the JSON report atomically")
    parser.add_argument("--selftest", action="store_true", help="verify the feature instrument")
    args = parser.parse_args(argv)
    if not args.selftest and not args.inputs:
        parser.error("at least one input image is required")
    if args.selftest and args.inputs:
        parser.error("--selftest cannot be combined with input images")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest()

    document = build_document(args.inputs)
    # A nonzero exit is the authoritative failure signal.  Publish no partial
    # rows as a second fail-closed guard for callers that forget to check it.
    payload = [] if document["errors"] else document["rows"]
    if args.json is not None:
        write_json(args.json, payload)
        print(
            f"wrote {args.json}: {document['summary']['succeeded']} succeeded, "
            f"{document['summary']['failed']} failed",
            file=sys.stderr,
        )
    else:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, allow_nan=False)
        sys.stdout.write("\n")

    if document["errors"]:
        for error in document["errors"]:
            print(f"{error['name']}: {error['message']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
