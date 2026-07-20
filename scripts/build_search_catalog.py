#!/usr/bin/env python3
"""Build static, resumable-search reports without calling network services."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


IDENTITY_KEYS = ("candidate_id", "id", "uuid", "recipe_hash", "canonical_hash", "hash")
ARCHIVE_CONTAINER_KEYS = ("elites", "archive", "cells", "entries", "items", "candidates")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_ndjson(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    """Return records, skipped malformed lines, and an incomplete-tail count."""
    if not path.exists():
        return [], 0, 0
    payload = path.read_bytes()
    raw_lines = payload.splitlines()
    terminated = payload.endswith((b"\n", b"\r"))
    records: list[dict[str, Any]] = []
    malformed = 0
    incomplete_tail = 0
    for index, raw_line in enumerate(raw_lines):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == len(raw_lines) - 1 and not terminated:
                incomplete_tail += 1
            else:
                malformed += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            malformed += 1
    return records, malformed, incomplete_tail


def read_archive(path: Path) -> tuple[Any, str | None]:
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, str(error)


def looks_like_candidate(value: dict[str, Any]) -> bool:
    if any(key in value for key in IDENTITY_KEYS):
        return True
    return any(key in value for key in ("recipe", "stages", "quality", "fitness", "metrics"))


def archive_records(value: Any, inherited_cell: str = "") -> list[dict[str, Any]]:
    """Extract candidate records/references from common MAP-Elites schemas."""
    if value is None:
        return []
    if isinstance(value, list):
        records: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            records.extend(archive_records(item, inherited_cell or str(index)))
        return records
    if not isinstance(value, dict):
        return []

    nested_candidate = value.get("candidate") or value.get("elite")
    if isinstance(nested_candidate, dict):
        merged = dict(nested_candidate)
        for key, item in value.items():
            if key not in ("candidate", "elite") and key not in merged:
                merged[key] = item
        if inherited_cell and not merged.get("archive_cell"):
            merged["archive_cell"] = inherited_cell
        return [merged]

    for key in ARCHIVE_CONTAINER_KEYS:
        if key in value:
            container = value[key]
            if isinstance(container, dict):
                records = []
                for cell, item in container.items():
                    records.extend(archive_records(item, str(cell)))
                return records
            return archive_records(container, inherited_cell)

    if looks_like_candidate(value):
        record = dict(value)
        if inherited_cell and not record.get("archive_cell"):
            record["archive_cell"] = inherited_cell
        return [record]

    records = []
    for cell, item in value.items():
        if isinstance(item, (dict, list)):
            records.extend(archive_records(item, str(cell)))
    return records


def nested(record: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = record
        found = True
        for component in path.split("."):
            if not isinstance(value, dict) or component not in value:
                found = False
                break
            value = value[component]
        if found and value is not None:
            return value
    return None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def candidate_identity(record: dict[str, Any], fallback: str) -> str:
    for key in IDENTITY_KEYS:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    recipe = record.get("recipe")
    if isinstance(recipe, dict):
        for key in ("canonical_hash", "recipe_hash", "hash", "id"):
            if recipe.get(key) not in (None, ""):
                return str(recipe[key])
    return fallback


def merge_candidate(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        elif value is not None:
            merged[key] = value
    return merged


def normalize(record: dict[str, Any], candidate_id: str, archived: bool) -> dict[str, Any]:
    quality = finite_number(
        nested(
            record,
            "quality",
            "quality.score",
            "quality.worst_case",
            "robust_quality",
            "fitness",
            "fitness.score",
            "score",
        )
    )
    novelty = finite_number(nested(record, "novelty", "metrics.novelty", "quality.novelty"))
    robustness = finite_number(
        nested(
            record,
            "robustness",
            "metrics.robustness",
            "metrics.min_input_changed_ratio",
            "quality.robustness",
            "worst_case_quality",
        )
    )
    fps = finite_number(
        nested(record, "performance_fps", "fps", "metrics.fps", "performance.fps", "performance.p95_fps")
    )
    if fps is None:
        process_ms = finite_number(nested(record, "metrics.mean_process_ms", "mean_process_ms"))
        if process_ms is not None and process_ms > 0:
            fps = 1000.0 / process_ms
    render_scale = finite_number(
        nested(record, "render_scale", "recipe.render_scale", "parameters.render_scale")
    )
    stages = nested(record, "stages", "recipe.stages")
    stage_count = nested(record, "stage_count", "recipe.stage_count")
    if stage_count is None and isinstance(stages, list):
        stage_count = len(stages)

    recipe_hash = nested(record, "recipe_hash", "canonical_hash", "recipe.canonical_hash", "recipe.hash")
    preview = nested(
        record,
        "preview_path",
        "preview",
        "video_path",
        "video",
        "thumbnail_path",
        "thumbnail",
        "artifacts.preview",
        "artifacts.video",
    )
    family = nested(record, "family", "generation", "recipe.family", "category")
    archive_cell = nested(record, "archive_cell", "cell", "cell_id", "descriptor_cell")
    status = nested(record, "status", "result", "gate_status")
    if status is None:
        if record.get("admitted") is True or archived:
            status = "elite"
        elif record.get("accepted") is True:
            status = "accepted"
        elif record.get("accepted") is False:
            status = "rejected"

    return {
        "candidate_id": candidate_id,
        "archived": archived,
        "archive_cell": "" if archive_cell is None else str(archive_cell),
        "family": "" if family is None else str(family),
        "quality": quality,
        "novelty": novelty,
        "robustness": robustness,
        "performance_fps": fps,
        "render_scale": render_scale,
        "stage_count": stage_count,
        "recipe_hash": "" if recipe_hash is None else str(recipe_hash),
        "preview_path": "" if preview is None else str(preview),
        "status": "" if status is None else str(status),
        "record": sanitize_json(record),
    }


def sanitize_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    def descending(value: Any) -> float:
        number = finite_number(value)
        return -(number if number is not None else -math.inf)

    return (
        0 if candidate["archived"] else 1,
        descending(candidate.get("quality")),
        descending(candidate.get("robustness")),
        descending(candidate.get("novelty")),
        candidate.get("candidate_id", ""),
    )


def media_url(raw_path: str, output_dir: Path) -> tuple[str, str] | None:
    if not raw_path or "://" in raw_path or raw_path.startswith("javascript:"):
        return None
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(output_dir.resolve())
        except ValueError:
            return None
    suffix = path.suffix.lower()
    if suffix not in (".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return None
    encoded = "/".join(quote(component) for component in path.parts if component not in (".", ".."))
    kind = "video" if suffix in (".mp4", ".mov", ".webm") else "image"
    return encoded, kind


def display_number(value: Any, digits: int = 4) -> str:
    number = finite_number(value)
    return "—" if number is None else f"{number:.{digits}g}"


def build_html(candidates: list[dict[str, Any]], metadata: dict[str, Any], output_dir: Path) -> str:
    cards: list[str] = []
    for rank, candidate in enumerate(candidates, 1):
        media = media_url(candidate.get("preview_path", ""), output_dir)
        media_markup = '<div class="media empty">No preview</div>'
        if media:
            url, kind = media
            safe_url = html.escape(url, quote=True)
            if kind == "video":
                media_markup = (
                    f'<video class="media" src="{safe_url}" controls muted loop preload="metadata"></video>'
                )
            else:
                media_markup = f'<img class="media" src="{safe_url}" loading="lazy" alt="Preset preview">'
        candidate_id = html.escape(candidate["candidate_id"])
        family = html.escape(candidate.get("family") or "unclassified")
        cell = html.escape(candidate.get("archive_cell") or "—")
        status = html.escape(candidate.get("status") or ("archive" if candidate["archived"] else "candidate"))
        cards.append(
            f"""
            <article class="card">
              {media_markup}
              <div class="body">
                <div class="rank">#{rank} · {status}</div>
                <h2>{candidate_id}</h2>
                <p>{family} · cell {cell}</p>
                <dl>
                  <div><dt>Quality</dt><dd>{display_number(candidate.get('quality'))}</dd></div>
                  <div><dt>Robustness</dt><dd>{display_number(candidate.get('robustness'))}</dd></div>
                  <div><dt>Novelty</dt><dd>{display_number(candidate.get('novelty'))}</dd></div>
                  <div><dt>FPS</dt><dd>{display_number(candidate.get('performance_fps'))}</dd></div>
                </dl>
              </div>
            </article>"""
        )

    render_width = metadata.get("render_width")
    render_height = metadata.get("render_height")
    render_context = ""
    if render_width and render_height:
        render_context = f" · {html.escape(str(render_width))}×{html.escape(str(render_height))}"
    if metadata.get("backend"):
        render_context += f" · {html.escape(str(metadata['backend']))}"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GLIC Metal Search Catalog</title>
  <style>
    :root {{ color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #080808; color: #eee; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 20px 28px; background: rgba(8,8,8,.94); border-bottom: 1px solid #292929; backdrop-filter: blur(12px); }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    header p, .body p {{ margin: 0; color: #aaa; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; padding: 24px; }}
    .card {{ overflow: hidden; background: #111; border: 1px solid #272727; border-radius: 12px; }}
    .media {{ display: block; width: 100%; aspect-ratio: 16/9; object-fit: cover; background: #050505; }}
    .media.empty {{ display: grid; place-items: center; color: #555; }}
    .body {{ padding: 16px; }}
    .rank {{ color: #88ff9e; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    h2 {{ margin: 7px 0; overflow: hidden; text-overflow: ellipsis; font: 600 16px ui-monospace, SFMono-Regular, Menlo, monospace; }}
    dl {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 15px 0 0; }}
    dl div {{ padding: 8px; background: #181818; border-radius: 6px; }}
    dt {{ color: #888; font-size: 11px; }}
    dd {{ margin: 3px 0 0; font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <header>
    <h1>GLIC Metal Search Catalog</h1>
    <p>{len(candidates)} listed · {metadata['archive_count']} archived · {metadata['candidate_record_count']} evaluated{render_context} · updated {html.escape(metadata['generated_at'])}</p>
  </header>
  <main>{''.join(cards) if cards else '<p>No completed candidates yet. The catalog can be regenerated while the search is running.</p>'}</main>
</body>
</html>
"""


def write_csv(path: Path, candidates: Iterable[dict[str, Any]]) -> None:
    fieldnames = [
        "rank",
        "candidate_id",
        "archived",
        "archive_cell",
        "family",
        "quality",
        "robustness",
        "novelty",
        "performance_fps",
        "render_scale",
        "stage_count",
        "recipe_hash",
        "preview_path",
        "status",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for rank, candidate in enumerate(candidates, 1):
                row = {key: candidate.get(key, "") for key in fieldnames if key != "rank"}
                row["rank"] = rank
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--candidates", type=Path, help="candidates.ndjson path")
    parser.add_argument("--archive", type=Path, help="archive.json path")
    parser.add_argument("--output-dir", type=Path, help="directory for generated reports")
    parser.add_argument("--limit", type=int, default=0, help="maximum listed candidates; 0 keeps all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        print("--limit must be zero or positive", file=sys.stderr)
        return 2

    run_dir = args.run_dir.expanduser().resolve()
    candidates_path = (args.candidates or run_dir / "candidates.ndjson").expanduser().resolve()
    archive_path = (args.archive or run_dir / "archive.json").expanduser().resolve()
    output_dir = (args.output_dir or run_dir).expanduser().resolve()

    records, malformed_lines, incomplete_tail = read_ndjson(candidates_path)
    archive, archive_error = read_archive(archive_path)
    extracted_archive = archive_records(archive)

    by_id: dict[str, dict[str, Any]] = {}
    source_order: list[str] = []
    for index, record in enumerate(records):
        candidate_id = candidate_identity(record, f"record-{index + 1:08d}")
        if candidate_id not in by_id:
            source_order.append(candidate_id)
            by_id[candidate_id] = record
        else:
            by_id[candidate_id] = merge_candidate(by_id[candidate_id], record)

    archive_ids: set[str] = set()
    for index, record in enumerate(extracted_archive):
        candidate_id = candidate_identity(record, f"archive-{index + 1:08d}")
        archive_ids.add(candidate_id)
        if candidate_id in by_id:
            by_id[candidate_id] = merge_candidate(by_id[candidate_id], record)
        else:
            source_order.append(candidate_id)
            by_id[candidate_id] = record

    selected_ids = list(archive_ids) if archive_ids else source_order
    candidates = [normalize(by_id[candidate_id], candidate_id, candidate_id in archive_ids) for candidate_id in selected_ids]
    archive_metadata = archive if isinstance(archive, dict) else {}
    archive_render_scale = finite_number(archive_metadata.get("render_scale"))
    for candidate in candidates:
        if candidate.get("render_scale") is None:
            candidate["render_scale"] = archive_render_scale
    candidates.sort(key=sort_key)
    if args.limit:
        candidates = candidates[: args.limit]

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "schema_version": 1,
        "generated_at": generated_at,
        "run_dir": str(run_dir),
        "candidates_path": str(candidates_path),
        "archive_path": str(archive_path),
        "candidate_record_count": len(records),
        "unique_candidate_count": len(by_id),
        "archive_count": len(archive_ids),
        "listed_count": len(candidates),
        "malformed_ndjson_lines": malformed_lines,
        "incomplete_ndjson_tail_lines": incomplete_tail,
        "archive_read_error": archive_error,
        "backend": archive_metadata.get("backend"),
        "render_scale": archive_render_scale,
        "render_width": archive_metadata.get("width"),
        "render_height": archive_metadata.get("height"),
    }
    catalog = {"metadata": metadata, "candidates": candidates}

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "leaderboard.csv", candidates)
    atomic_write_text(
        output_dir / "catalog.json",
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    atomic_write_text(output_dir / "index.html", build_html(candidates, metadata, output_dir))

    if malformed_lines:
        print(f"warning: skipped {malformed_lines} malformed NDJSON line(s)", file=sys.stderr)
    if incomplete_tail:
        print("note: ignored an incomplete final NDJSON line while the search was writing", file=sys.stderr)
    if archive_error:
        print(f"warning: archive.json was not readable; candidates fallback used: {archive_error}", file=sys.stderr)
    print(f"wrote {len(candidates)} candidates to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
