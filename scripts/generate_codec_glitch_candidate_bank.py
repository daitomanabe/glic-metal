#!/usr/bin/env python3
"""Render, rank, and publish a bounded Codec Glitch candidate bank."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


MAX_CANDIDATES = 50


def candidate(
    name: str, effect: str, amount: float, rate: float, feedback: float
) -> dict[str, Any]:
    return {
        "name": name,
        "effect": effect,
        "amount": amount,
        "rate": rate,
        "feedback": feedback,
    }


CANDIDATES = [
    candidate("qp_soft_scan", "qp_pump", 0.38, 0.18, 0.15),
    candidate("qp_mid_pulse", "qp_pump", 0.62, 0.48, 0.25),
    candidate("qp_hard_wave", "qp_pump", 0.86, 0.72, 0.35),
    candidate("qp_erratic", "qp_pump", 0.98, 0.93, 0.45),
    candidate("bitrate_fine", "bitrate_crush", 0.35, 0.20, 0.20),
    candidate("bitrate_pulse", "bitrate_crush", 0.58, 0.45, 0.35),
    candidate("bitrate_blocks", "bitrate_crush", 0.82, 0.25, 0.45),
    candidate("bitrate_meltdown", "bitrate_crush", 0.96, 0.78, 0.55),
    candidate("dropout_sparse", "slice_dropout", 0.35, 0.18, 0.30),
    candidate("dropout_traveling", "slice_dropout", 0.55, 0.65, 0.42),
    candidate("dropout_dense", "slice_dropout", 0.78, 0.38, 0.55),
    candidate("dropout_storm", "slice_dropout", 0.93, 0.88, 0.70),
    candidate("transplant_narrow", "slice_transplant", 0.32, 0.22, 0.40),
    candidate("transplant_offset", "slice_transplant", 0.50, 0.65, 0.62),
    candidate("transplant_weave", "slice_transplant", 0.70, 0.40, 0.82),
    candidate("transplant_mosaic", "slice_transplant", 0.90, 0.86, 0.95),
    candidate("pframe_skip", "pframe_loss", 0.28, 0.20, 0.30),
    candidate("pframe_stagger", "pframe_loss", 0.48, 0.45, 0.45),
    candidate("pframe_stutter", "pframe_loss", 0.70, 0.68, 0.60),
    candidate("pframe_freeze_bursts", "pframe_loss", 0.90, 0.90, 0.80),
    candidate("idr_rare", "idr_starvation", 0.30, 0.20, 0.40),
    candidate("idr_drift", "idr_starvation", 0.50, 0.40, 0.55),
    candidate("idr_drought", "idr_starvation", 0.72, 0.65, 0.70),
    candidate("idr_collapse", "idr_starvation", 0.92, 0.90, 0.85),
    candidate("payload_chromatic", "payload_xor", 0.35, 0.25, 0.25),
    candidate("payload_tiles", "payload_xor", 0.55, 0.52, 0.40),
    candidate("payload_mosaic", "payload_xor", 0.75, 0.30, 0.55),
    candidate("payload_rupture", "payload_xor", 0.94, 0.83, 0.70),
    candidate("timewarp_echo", "reference_timewarp", 0.30, 0.20, 0.45),
    candidate("timewarp_jump", "reference_timewarp", 0.52, 0.40, 0.65),
    candidate("timewarp_deep", "reference_timewarp", 0.72, 0.64, 0.85),
    candidate("timewarp_nonlinear", "reference_timewarp", 0.92, 0.90, 0.98),
    candidate("feedback_haze", "codec_feedback", 0.28, 0.20, 0.45),
    candidate("feedback_trails", "codec_feedback", 0.50, 0.40, 0.68),
    candidate("feedback_recursion", "codec_feedback", 0.72, 0.62, 0.86),
    candidate("feedback_overload", "codec_feedback", 0.92, 0.84, 0.98),
    candidate("cascade_soft", "generation_cascade", 0.35, 0.18, 0.30),
    candidate("cascade_aged", "generation_cascade", 0.55, 0.40, 0.45),
    candidate("cascade_deep", "generation_cascade", 0.76, 0.62, 0.62),
    candidate("cascade_collapse", "generation_cascade", 0.94, 0.86, 0.80),
    candidate("resolution_half", "resolution_hop", 0.32, 0.20, 0.30),
    candidate("resolution_pulse", "resolution_hop", 0.52, 0.55, 0.40),
    candidate("resolution_quarter", "resolution_hop", 0.78, 0.35, 0.55),
    candidate("resolution_staircase", "resolution_hop", 0.95, 0.82, 0.70),
    candidate("chroma_subtle", "chroma_codec_echo", 0.30, 0.18, 0.45),
    candidate("chroma_drift", "chroma_codec_echo", 0.52, 0.38, 0.65),
    candidate("chroma_split", "chroma_codec_echo", 0.75, 0.62, 0.86),
    candidate("chroma_flood", "chroma_codec_echo", 0.94, 0.85, 0.98),
    candidate("pframe_syncopated", "pframe_loss", 0.62, 0.95, 0.52),
    candidate("timewarp_rewind_burst", "reference_timewarp", 0.86, 0.12, 0.94),
]


def validate_candidates() -> None:
    if len(CANDIDATES) != MAX_CANDIDATES:
        raise ValueError(f"candidate bank must contain {MAX_CANDIDATES} entries")
    names = [entry["name"] for entry in CANDIDATES]
    if len(names) != len(set(names)):
        raise ValueError("candidate names must be unique")
    effects = {entry["effect"] for entry in CANDIDATES}
    if len(effects) != 12:
        raise ValueError("candidate bank must cover all 12 codec effects")
    for entry in CANDIDATES:
        for key in ("amount", "rate", "feedback"):
            value = entry[key]
            if not isinstance(value, float) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{entry['name']} has invalid {key}")


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def write_preview_html(path: Path) -> None:
    html = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GLIC Metal — 50 Codec Glitch Candidates</title>
<style>
:root{color-scheme:dark;--bg:#090909;--panel:#121212;--line:#333;--text:#f3f3ed;--muted:#aaa;--signal:#d8ff45}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;z-index:3;display:flex;gap:1rem;align-items:center;justify-content:space-between;padding:1rem 1.25rem;background:#090909ed;border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}h1{margin:0;font-size:1rem;letter-spacing:.08em}.summary{color:var(--muted);font:12px ui-monospace,SFMono-Regular,monospace}.actions{display:flex;flex-wrap:wrap;gap:.5rem}button{padding:.5rem .75rem;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--text);cursor:pointer}main{padding:1rem}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1rem}article{overflow:hidden;border:1px solid var(--line);background:var(--panel)}article.selected{border-color:var(--signal);box-shadow:0 0 0 1px var(--signal) inset}video{display:block;width:100%;aspect-ratio:16/9;background:#000;object-fit:cover}.body{padding:.8rem}.title-row{display:flex;align-items:center;justify-content:space-between;gap:.75rem}h2{margin:0;font:600 13px ui-monospace,SFMono-Regular,monospace}label{display:flex;align-items:center;gap:.4rem;color:var(--signal);font-size:13px;cursor:pointer}input{accent-color:var(--signal)}.effect{margin-top:.3rem;color:var(--muted);font:11px ui-monospace,SFMono-Regular,monospace}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:.3rem;margin-top:.65rem}.metric{padding:.42rem;background:#090909}.metric span{display:block;color:var(--muted);font:9px ui-monospace,SFMono-Regular,monospace;text-transform:uppercase}.metric strong{font:12px ui-monospace,SFMono-Regular,monospace}.pass{color:var(--signal)}.error{padding:2rem;color:#ff8d8d;white-space:pre-wrap}@media(max-width:600px){header{align-items:flex-start;flex-direction:column}.grid{grid-template-columns:1fr}}
</style></head><body><header><div><h1>GLIC—METAL / CODEC GLITCH 50</h1><div id="summary" class="summary">ranking.json を読み込み中…</div></div><div class="actions"><button id="all">全て選択</button><button id="none">全て解除</button><button id="export">選択JSONをコピー</button></div></header><main><div id="grid" class="grid"></div></main>
<script>
const storageKey='glic-metal-codec-selection-v2';const selected=new Set(JSON.parse(localStorage.getItem(storageKey)||'[]'));let candidates=[];const save=()=>localStorage.setItem(storageKey,JSON.stringify([...selected]));const fmt=(v,d=2)=>Number.isFinite(Number(v))?Number(v).toFixed(d):'—';
function summary(){const eligible=candidates.filter(x=>x.performance?.hard_gate_passed).length;document.querySelector('#summary').textContent=`${candidates.length} candidates / ${eligible} realtime eligible / ${selected.size} selected`}
function render(){const grid=document.querySelector('#grid');grid.replaceChildren();for(const item of candidates){const perf=item.performance||{},m=item.metrics||{},c=item.controls||{};const card=document.createElement('article');card.classList.toggle('selected',selected.has(item.label));card.innerHTML=`<video src="./${item.label}.mp4" controls loop muted playsinline preload="metadata"></video><div class="body"><div class="title-row"><h2>${item.rank}. ${item.label}</h2><label><input type="checkbox" ${selected.has(item.label)?'checked':''}>採用</label></div><div class="effect">${item.effect} · A ${fmt(c.amount)} · R ${fmt(c.rate)} · F ${fmt(c.feedback)}</div><div class="metrics"><div class="metric"><span>gate</span><strong class="${perf.hard_gate_passed?'pass':''}">${perf.hard_gate_passed?'PASS':'FAIL'}</strong></div><div class="metric"><span>engine fps</span><strong>${fmt(perf.codec_engine_fps,1)}</strong></div><div class="metric"><span>p95 ms</span><strong>${fmt(perf.latency_p95_ms,2)}</strong></div><div class="metric"><span>changed</span><strong>${fmt(m.changed_ratio*100,1)}%</strong></div><div class="metric"><span>MAE</span><strong>${fmt(m.mae_8bit,2)}</strong></div><div class="metric"><span>diversity</span><strong>${fmt(item.scores?.selection_diversity,3)}</strong></div><div class="metric"><span>intentional</span><strong>${perf.intentional_repeat_frames??0}</strong></div><div class="metric"><span>errors</span><strong>${perf.codec_errors??'—'}</strong></div></div></div>`;const box=card.querySelector('input');box.addEventListener('change',()=>{box.checked?selected.add(item.label):selected.delete(item.label);card.classList.toggle('selected',box.checked);save();summary()});grid.append(card)}summary()}
document.querySelector('#all').addEventListener('click',()=>{candidates.forEach(x=>selected.add(x.label));save();render()});document.querySelector('#none').addEventListener('click',()=>{selected.clear();save();render()});document.querySelector('#export').addEventListener('click',async e=>{const chosen=candidates.filter(x=>selected.has(x.label)).map(x=>({name:x.label,effect:x.effect,...x.controls}));await navigator.clipboard.writeText(JSON.stringify({codec_candidates:chosen},null,2));const b=e.currentTarget,o=b.textContent;b.textContent='コピーしました';setTimeout(()=>b.textContent=o,1200)});fetch('./ranking.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json()}).then(d=>{candidates=d.ranking||[];render()}).catch(e=>{document.querySelector('#grid').innerHTML=`<div class="error">ranking.json の読み込みに失敗しました。\n${e}</div>`});
</script></body></html>
"""
    path.write_text(html, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate up to 50 ranked realtime Codec Glitch videos."
    )
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("test-videos/codec-glitch"))
    parser.add_argument("--filter-bin", type=Path, default=Path("build/glic_codec_glitch_filter"))
    parser.add_argument("--limit", type=int, default=MAX_CANDIDATES)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_candidates()
    if args.selftest:
        print(f"PASS codec candidate bank candidates={len(CANDIDATES)} effects=12")
        return 0
    if args.input is None or not args.input.is_file():
        raise SystemExit("input video is required and must exist")
    if not 1 <= args.limit <= MAX_CANDIDATES:
        raise SystemExit(f"--limit must be in [1, {MAX_CANDIDATES}]")
    if args.width < 960 or args.height < 540 or args.fps < 20:
        raise SystemExit("candidate bank requires at least 960x540 and 20 fps")
    if not args.filter_bin.is_file():
        raise SystemExit(f"codec filter does not exist: {args.filter_bin}")

    root = Path(__file__).resolve().parent.parent
    process_script = root / "scripts" / "process_video.py"
    evaluator = root / "scripts" / "evaluate_codec_glitch_videos.py"
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = CANDIDATES[: args.limit]
    common = [
        "--width", str(args.width), "--height", str(args.height),
        "--fps", str(args.fps), "--filter-bin", str(args.filter_bin.resolve()),
    ]
    overwrite = ["--overwrite"] if args.overwrite else []

    run([
        sys.executable, str(process_script), str(args.input.resolve()),
        str(output_dir / "control.mp4"), "--processing-mode", "compat_realtime",
        "--passthrough", "--width", str(args.width), "--height", str(args.height),
        "--fps", str(args.fps), "--report", str(output_dir / "control.json"),
        *overwrite,
    ])

    manifest = []
    for index, entry in enumerate(selected):
        seed = 0x47C50000 + index * 0x9E37 + 1
        if seed > 0xFFFFFFFF:
            raise RuntimeError("candidate seed exceeds the process-video ABI")
        video = output_dir / f"{entry['name']}.mp4"
        report = output_dir / f"{entry['name']}.json"
        run([
            sys.executable, str(process_script), str(args.input.resolve()), str(video),
            "--processing-mode", "codec_glitch", "--codec-effect", entry["effect"],
            "--codec-amount", str(entry["amount"]), "--codec-rate", str(entry["rate"]),
            "--codec-feedback", str(entry["feedback"]), "--seed", hex(seed),
            *common, "--report", str(report), *overwrite,
        ])
        manifest.append({**entry, "seed": seed, "video": video.name, "report": report.name})

    evaluation = [
        sys.executable, str(evaluator), "--control", str(output_dir / "control.mp4"),
        "--output-json", str(output_dir / "ranking.json"),
        "--output-md", str(output_dir / "ranking.md"),
        "--min-fps", "20", "--required-width", str(args.width),
        "--required-height", str(args.height), "--minimum-frames", "120",
    ]
    for entry in manifest:
        evaluation.extend([
            "--candidate", str(output_dir / entry["video"]),
            "--report", str(output_dir / entry["report"]),
            "--label", entry["name"],
        ])
    run(evaluation)
    (output_dir / "candidate-bank.json").write_text(
        json.dumps({"schema": "glic-codec-candidate-bank-v1", "candidates": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    write_preview_html(output_dir / "index.html")
    print(f"PASS candidate bank count={len(manifest)} preview={output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
