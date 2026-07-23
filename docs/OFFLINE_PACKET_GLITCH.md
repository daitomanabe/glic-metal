# Offline Packet Glitch Lab

[日本語](#日本語) | [English](#english)

## 日本語

Offline Packet Glitch Labは、圧縮済みpacket、NAL unit、OBU、timestampを意図的に
破損し、復号できたframeだけを安全なpreviewへ救済する実験用経路です。
VideoToolboxのリアルタイムC ABIとは別系統で、リアルタイム性能を主張しません。

### リアルタイム経路との境界

| 経路 | 入力 | 圧縮データ破損 | 実行場所 | realtime |
|---|---|---:|---|---:|
| `glic_codec_glitch_context` | clean `CVPixelBufferRef` | しない | host process | 960×540・20fps gateあり |
| Offline Packet Lab | file | する場合がある | 制限付きsubprocess | 非対応 |

破損streamはhost application内でdecodeしません。runnerは各FFmpeg処理を独立した
process groupで実行し、timeout、CPU時間、最大出力容量、file descriptor数を制限します。
救済decodeは1 threadです。失敗・timeout・decoder診断はJSONとstage logへ残ります。

### 8種類のeffect

| Effect | H.264 | HEVC | AV1 | VP9 | ProRes | 操作 |
|---|---:|---:|---:|---:|---:|---|
| `packet_bit_rot` | ✓ | ✓ | ✓ | ✓ | ✓ | 選択packetのbyteを破損 |
| `gop_amputation` | ✓ | ✓ | ✓ | ✓ | — | 最初以外のkey packetを除去 |
| `packet_dropout_score` | ✓ | ✓ | ✓ | ✓ | ✓ | 決定的な周期で非key packetを欠落 |
| `timestamp_fracture` | ✓ | ✓ | ✓ | ✓ | ✓ | PTSを前後へずらして重複・欠落を生成 |
| `nal_obu_surgery` | ✓ | ✓ | ✓ | ✓ | — | 選択NAL/OBU unitを除去 |
| `header_hallucination` | ✓ | ✓ | ✓ | ✓ | — | aspect・range・色metadataを変更 |
| `packet_transplant` | ✓ | ✓ | ✓ | ✓ | ✓ | 同一codecでencodeした別動画のpacket区間を移植 |
| `vp9_superframe_shuffle` | — | — | — | ✓ | — | superframe分解後にPTSを再配置 |

ProResはintra-frame codecなのでGOP切断の対象にしません。利用可能な正規組み合わせは
`resources/offline-codec-effects.json`を参照してください。実装はFFmpegの
[bitstream filters](https://ffmpeg.org/ffmpeg-bitstream-filters.html)を使用し、
復号前の圧縮データへ作用します。

### 実行

```bash
python3 scripts/process_offline_packet_glitch.py input.mov output.mp4 \
  --codec h264 \
  --effect packet_bit_rot \
  --amount 0.68 \
  --width 960 --height 540 --fps 30 --max-frames 180
```

出力:

- `output.mp4` — 救済できたframeだけを再encodeしたH.264 review preview
- `output.mp4.json` — codec、effect、frame生存率、診断数、hash、probe、全command
- `output.mp4.packet-stages/` — 元bitstream、破損bitstream、FFV1救済映像、全log

previewが生成されても、元の破損bitstreamが一般的なplayerで再生できるという意味では
ありません。`qualified_preview`は2 frame以上を救済してreview MP4を作れたことだけを
表します。

### 異なる長さを許容する画像・時間評価

packet欠落後はframe数が一致しないため、通常のaligned-frame評価器は使いません。
専用評価器は各動画の正規化された時間位置を比較し、視覚差と時間差に生存率を加えて
rankingします。

```bash
python3 scripts/evaluate_offline_packet_glitches.py input.mov \
  --control control.mp4 \
  --candidate bit_rot=bit-rot.mp4 \
  --candidate timestamp=timestamp.mp4 \
  --samples 24 \
  --output-json offline-evaluation.json \
  --output-md offline-evaluation.md \
  --heatmap offline-evaluation.png
```

採用gateは次のいずれかの変化を要求します。

- `VISIBLE`または`STRONG`の空間差
- 8%以上のframe欠落
- 平均2.5/255以上の時間変化

さらにdecode生存率3%以上と`qualified_preview=true`が必須です。rankingは生存率で
重み付けし、ほぼ空のstreamが「最も異なる」という理由だけで1位になることを防ぎます。

## English

The Offline Packet Glitch Lab intentionally damages compressed packets,
NAL units, OBUs, or timestamps and salvage-decodes whatever frames remain. It
is separate from the clean VideoToolbox realtime C ABI and makes no realtime
claim.

Run `process_offline_packet_glitch.py` only as an isolated file workflow. Every
FFmpeg stage has a timeout plus CPU, file-size, and descriptor limits; damaged
decode is single-threaded. The runner retains the original and damaged
bitstreams, a lossless salvaged intermediate, logs, hashes, probes, and a JSON
report.

The eight canonical effects and codec support matrix are defined in
`resources/offline-codec-effects.json`. ProRes is excluded from GOP amputation
because it is intra-frame. Unsupported combinations fail closed.

Use `evaluate_offline_packet_glitches.py` instead of the aligned-frame
evaluator. It samples normalized timeline positions, accepts unequal decoded
lengths, measures spatial and temporal differences, records decode survival,
and downweights nearly empty streams in its ranking.
