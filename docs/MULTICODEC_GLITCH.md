# Multi-codec encode/decode glitch

[日本語](#日本語) | [English](#english)

## 日本語

GLIC Metalには、実際のcodec encode/decodeを通す2種類の経路があります。両者を
「すべてGPU realtime」とは扱いません。

圧縮packet自体を破損する3つ目のfile経路は
[Offline Packet Glitch Lab](OFFLINE_PACKET_GLITCH.md)に分離しています。

| Codec | Encode / decode | 経路 | realtime判定 |
|---|---|---|---|
| H.264 / AVC | VideoToolbox | C/C++非同期API、`process_video.py` | 実測JSONで20/30 fps判定 |
| HEVC / H.265 | VideoToolbox | C/C++非同期API、`process_video.py` | 実測JSONで20/30 fps判定 |
| ProRes 422 | VideoToolbox | C/C++非同期API、`process_video.py` | 実測JSONで20/30 fps判定 |
| AV1 | FFmpeg `libaom-av1` / `libdav1d` | offline generation runner | realtimeを主張しない |
| VP9 | FFmpeg `libvpx-vp9` | offline generation runner | realtimeを主張しない |
| AV2 | AOMedia AVM v1.0.0 `avmenc` / `avmdec` | 公式reference runner | realtimeを主張しない |
| VVC / H.266 | Fraunhofer VVenC v1.14.0 / FFmpeg VVC decode | 公式reference runner | realtimeを主張しない |
| Theora | FFmpeg `libtheora` / `theora` | offline generation runner | realtimeを主張しない |
| Dirac / VC-2 | FFmpeg `vc2` / `dirac` | offline generation runner | realtimeを主張しない |

AV2は名前だけの代替codecへ置換しません。公式AVM v1.0.0が無ければfail-closed
します。VVCも公式VVenCが無ければfail-closedします。AV1 / AV2 / VP9 / VVC /
Theora / Diracは各generationのbitstream、SHA-256、codec probe、
encode/decode時間を`.codec-stages/`とJSONへ残します。最終MP4はレビュー用で、
H.264 previewです。実際の対象codec bitstreamはstage directory内にあります。

### native VideoToolbox

`glic_codec_glitch_config.codec`を設定してからprepareします。ゼロ初期化と
`glic_codec_glitch_config_init()`の既定値は互換性のためH.264です。

```c
glic_codec_glitch_config config;
glic_codec_glitch_config_init(&config);
config.width = 960;
config.height = 540;
config.frames_per_second = 30;
config.codec = GLIC_CODEC_GLITCH_CODEC_HEVC; /* or PRORES_422 */

if (glic_codec_glitch_prepare(codec_context, &config) !=
    GLIC_CODEC_GLITCH_OK) {
  log_error(glic_codec_glitch_get_last_error(codec_context));
}
```

CLI:

```bash
python3 scripts/process_video.py input.mov output-hevc.mp4 \
  --processing-mode codec_glitch \
  --codec-format hevc \
  --codec-effect generation_cascade \
  --codec-generations 3 \
  --width 960 --height 540 --fps 30 --overwrite
```

H.264とHEVCは時間圧縮codecです。ProResはintra-frame codecなので
`pframe_loss`は意味を持たず、QP / bitrate propertyもhardware実装により
無視される場合があります。ProResでは`generation_cascade`、
`slice_dropout`、`slice_transplant`、`payload_xor`、`resolution_hop`、
`chroma_codec_echo`、`temporal_polyphony`、`intra_cannibalism`、
`residual_rift`、`codec_grain_synth`、`recursive_codec_skin`、
`concealment_choreography`を優先してください。

### AV1 / AV2 / VP9 / VVC / Theora / Dirac generation runner

```bash
# FFmpeg full build: libaom, libdav1d, libvpx が必要
python3 scripts/process_multicodec_glitch.py input.mov output-av1.mp4 \
  --codec av1 --effect temporal_echo --generations 2 \
  --width 960 --height 540 --fps 30

python3 scripts/process_multicodec_glitch.py input.mov output-vp9.mp4 \
  --codec vp9 --effect residual_noise --generations 3

# AV2 official reference toolsをpinned tag/commitからbuild
python3 scripts/build_av2_reference.py
python3 scripts/process_multicodec_glitch.py input.mov output-av2.mp4 \
  --codec av2 --effect chroma_drift --generations 2 \
  --width 480 --height 270 --fps 15

# VVC official reference toolsをpinned tag/commitからbuild
python3 scripts/build_vvc_reference.py
python3 scripts/process_multicodec_glitch.py input.mov output-vvc.mp4 \
  --codec vvc --effect generation_cascade --generations 2

python3 scripts/process_multicodec_glitch.py input.mov output-theora.mp4 \
  --codec theora --effect temporal_echo --generations 2
python3 scripts/process_multicodec_glitch.py input.mov output-dirac.mp4 \
  --codec dirac --effect generation_cascade --generations 2
```

共通effectは次の4種類です。

- `generation_cascade`: 実codecの世代劣化
- `temporal_echo`: decode後の3-frame時間混合を次世代encodeへ戻す
- `chroma_drift`: decode後のCb/Crずれを次世代encodeへ戻す
- `residual_noise`: decode後の時間変動noiseを次世代encodeへ戻す

`--work-dir`を省略すると`output.mp4.codec-stages/`へ全bitstreamとdecode
中間映像を保存します。Dirac/VC-2は内部rasterを32×16 sliceへ整列して全灰色化を
防ぎ、review previewを指定寸法へ戻します。AV2とVVCは遅いため、探索時は`--max-frames`と
1/4解像度を使用してください。

### capability probe

```bash
python3 scripts/probe_multicodec_capabilities.py \
  --output test-videos/multicodec-capabilities.json
```

このprobeはFFmpegの実encoder/decoder一覧、AVM executable、VideoToolboxの
1-frame encode/decodeに加え、Offline Packet Labが必要とするbitstream filterと
codec/effect組み合わせを検査します。利用可否をcodec名から推測しません。

## English

GLIC Metal provides two honest multi-codec paths:

- The asynchronous C/C++ API uses real VideoToolbox encode/decode for H.264,
  HEVC, and ProRes 422. Each performance report measures rather than assumes
  20/30 fps eligibility.
- The offline generation runner uses FFmpeg for AV1/VP9/Theora/Dirac, official
  AVM v1.0.0 for AV2, and official Fraunhofer VVenC v1.14.0 for VVC. It
  retains every compressed generation and does not claim realtime performance.

Use `glic_codec_glitch_config.codec` or `process_video.py --codec-format` for
the native formats. Use `process_multicodec_glitch.py` for a common entry point
covering AV1, AV2, HEVC, VP9, ProRes, VVC, Theora, and Dirac. AV2 and VVC
fail closed when their pinned official tools are missing; neither is silently
substituted with another codec.

The review MP4 is deliberately separate from the compressed evidence. For
the offline codecs, inspect the persistent `.codec-stages/` directory and JSON
stage records for codec identity, bitstream SHA-256, size, encode/decode
duration, and generation count.

For packet, NAL/OBU, and timestamp damage, use the separate
[Offline Packet Glitch Lab](OFFLINE_PACKET_GLITCH.md). It runs damaged decode
under process and resource limits and never claims realtime eligibility.
