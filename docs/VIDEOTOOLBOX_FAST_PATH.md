# VideoToolbox Fast Path

[日本語](#日本語) | [English](#english)

This document is the implementation and integration contract for the
VideoToolbox/NV12/Metal realtime path. Effect semantics and the honest
compressed-domain boundary remain documented in
[CODEC_GLITCH.md](CODEC_GLITCH.md).

## 日本語

### 目的と実装範囲

Fast Pathは、H.264 / HEVC / ProRes 422のhardware encode/decode、NV12
pixel buffer、`CVMetalTextureCache`、1回のfused Metal computeを接続し、
Codec Glitch 36 effectを低遅延で処理します。圧縮payloadのmotion vectorや
transform coefficientを直接編集する経路ではありません。

公開ABIはversion 1のままです。既存structの予約領域を使ってpixel pathと実行時
evidence flagを追加したため、既存binaryとのstruct sizeを維持しています。

| Phase | 実装 |
|---|---|
| 0 | queue / encode / sample / decode / post / deliveryを個別計測し、実動画baselineを保存 |
| 1 | encoderが返した`CMSampleBufferRef`を再構築・payload copyせずdecoderへ渡す |
| 2 | IOSurface-backed pool、最大in-flight、履歴、frame option、QP値の確保を有界化・再利用 |
| 3 | encode/decodeを420v NV12へ統一し、planeを`CVMetalTextureCache`でMetal textureへmap |
| 4 | 36 effectを1つのfused Metal kernelへ統合 |
| 5 | decode callbackをMetal commit直後に戻し、GPU completion、watchdog、順序付き出力を非同期化 |
| 6 | 36 effect × 3 codec × 2解像度を実動画で検証し、catalog、SDK、資料へ契約を公開 |

### Pixel path

`glic_codec_glitch_config.pixel_path`を`prepare`前に設定します。

| 値 | 動作 |
|---|---|
| `GLIC_CODEC_GLITCH_PIXEL_PATH_AUTO` | NV12/Metalを優先し、resourceまたはsessionを作れなければBGRA互換へfallback |
| `GLIC_CODEC_GLITCH_PIXEL_PATH_NV12_METAL` | NV12、IOSurface、texture cache、fused Metalを必須化。準備できなければfail closed |
| `GLIC_CODEC_GLITCH_PIXEL_PATH_BGRA_COMPATIBILITY` | 従来のBGRA/Core Image互換経路を明示 |

```c
glic_codec_glitch_config config;
glic_codec_glitch_config_init(&config);
config.width = 960;
config.height = 540;
config.frames_per_second = 30;
config.codec = GLIC_CODEC_GLITCH_CODEC_HEVC;
config.pixel_path = GLIC_CODEC_GLITCH_PIXEL_PATH_NV12_METAL;

glic_codec_glitch_status status =
    glic_codec_glitch_prepare(codec_context, &config);
if (status != GLIC_CODEC_GLITCH_OK) {
  /* Required fast pathは互換経路へ黙って落ちない。 */
  report(glic_codec_glitch_get_last_error(codec_context));
}
```

`420v`のfull-size `CVPixelBufferRef`入力はBGRA stagingをせず、そのまま
VideoToolbox encoderへ渡します。32BGRA入力も利用できますが、encode前にMetal
kernelで420vへ変換するため、入力境界のzero-copyではありません。

decoderが返す420vはcopyせず、Y planeを`R8Unorm`、CbCr planeを`RG8Unorm`として
texture cacheから参照します。fused kernelは現在の2 plane、近いBGRA履歴、遠い
BGRA履歴を読み、公開契約の32BGRA出力を1枚生成します。従って「zero-copy」が
意味する範囲は次の通りです。

- 420v host input → encoder: stagingなし。
- encoder sample → decoder: compressed payload copy/rebuildなし。
- decoder 420v → Metal effect: decoded pixel copyなし。
- Metal effect → public output: 互換性のため新しい32BGRA output bufferを1枚生成。

### 非同期処理と順序

`submit`は入力をretainしてprivate encode queueへ渡し、in-flight上限に達すると
`GLIC_CODEC_GLITCH_BACKPRESSURE`を返します。待機してcapture callbackを塞がないで
ください。

decode callbackはMetal command bufferをcommitした時点で戻ります。GPU完了は
steady 80ms、codec warmup 300msのdeadlineで監視します。完了順が入れ替わっても、
callback/pollにはengineが受理したsubmission順で出します。`frame_index`自体は
ホストの値を保持するため、単調値を渡してください。

`glic_codec_glitch_flush()`はcodec contextだけでなく未完了Metal commandも待ちます。
context破棄後のlate completionはowner guardで破棄されます。

### 実行時に確認するflag

`AUTO`を使うhostはprepare成功だけでFast Path有効と判断してはいけません。
`glic_codec_glitch_get_stats()`で次をすべて確認します。

```c
glic_codec_glitch_stats stats;
glic_codec_glitch_stats_init(&stats);
glic_codec_glitch_get_stats(codec_context, &stats);

bool fast_path_active =
    stats.nv12_metal_fast_path &&
    stats.metal_texture_cache &&
    stats.fused_metal_effects &&
    stats.asynchronous_metal_delivery &&
    stats.ordered_delivery;
```

`gpu_timeouts`はC++/filter JSONの詳細統計に記録されます。C ABI version 1の
`glic_codec_glitch_stats`には追加していないため、完全な認証reportが必要な場合は
raw-video filterまたはC++ APIを使います。

### 実動画検証

必須gateは960×540以上、20fps以上、p95 50ms以下、hardware encoder/decoder、
frame数維持、GPU timeout 0、非意図的fallback/error/drop 0です。

2026-07-25のApple M5 Max実測では、FFmpegのNV12 raw出力を420vへ直接入れ、
120 frameの同一実写入力を使い、
36 effect × H.264/HEVC/ProRes 422 × 960×540/1920×1080の216条件がすべて
合格しました。

| codec / resolution | 36 effect中の最低processing fps | 最大p95 |
|---|---:|---:|
| H.264 / 960×540 | 102.375 | 13.640ms |
| H.264 / 1920×1080 | 53.806 | 19.393ms |
| HEVC / 960×540 | 99.013 | 12.171ms |
| HEVC / 1920×1080 | 52.043 | 20.346ms |
| ProRes 422 / 960×540 | 175.728 | 6.366ms |
| ProRes 422 / 1920×1080 | 99.149 | 11.130ms |

最も遅い条件はHEVC 1920×1080の`generation_cascade`で52.043fps、
p95 20.346msでした。これはこのmachineと入力での測定値であり、別Macやhost全体の
保証値ではありません。

再検証:

```bash
python3 scripts/validate_videotoolbox_fast_path.py input.mov \
  --filter-bin build/glic_codec_glitch_filter \
  --output-dir validation/videotoolbox-fast-path
```

runnerは途中結果を再利用でき、`--force`で再レンダーします。各cellのMP4、JSON、
logと、全体の`summary.json` / `summary.md`を残します。各reportは
`input_pixel_format=nv12_420v`と`direct_420v_input=true`も必須にします。
visual diversityは同じreportを
`scripts/evaluate_codec_glitch_videos.py`へ渡して評価できます。

`process_video.py --processing-mode codec_glitch`も既定でこの直接NV12入口を
使います。raw filter単体では後方互換のためBGRAが既定なので、直接経路は
`--input-pixel-format nv12 --pixel-path nv12`を明示します。

### SDK組み込みチェック

- `GlicMetalResources.bundle`の`glic_realtime.metallib`をapp targetへ含める。
- Xcode hostはMetal、CoreVideo、CoreMedia、VideoToolbox、CoreImageをlinkする。
- `prepare`はcontrol/background queueで一度だけ行う。
- captureから420vが得られる場合は、そのIOSurface-backed bufferを直接submitする。
- 32BGRA入力は対応するが、Metalによる420v変換が1段入る。
- `BACKPRESSURE`では入力をdropし、待たない。
- `AUTO`ではstats flagを記録し、BGRA compatibility fallbackをUI/telemetryへ出す。
- 終了時はflushしてからcontextをdestroyする。
- hostのcapture、表示、他effectを含めて20fps / p95 50msを再測定する。

## English

### Contract

The Fast Path connects hardware H.264/HEVC/ProRes 422 encode/decode, video-range
NV12 pixel buffers, `CVMetalTextureCache`, and one fused Metal compute dispatch.
It does not directly edit compressed motion vectors or transform coefficients.
ABI version 1 and public struct sizes remain unchanged.

Set `glic_codec_glitch_config.pixel_path` before prepare:

- `AUTO` prefers NV12/Metal and falls back to BGRA/Core Image when the runtime
  cannot create the required resources or session.
- `NV12_METAL` requires the fast path and fails closed.
- `BGRA_COMPATIBILITY` explicitly selects the previous compatibility path.

A full-size video-range NV12 (`420v`) host input reaches the encoder without a
BGRA staging buffer. A 32BGRA host input remains supported and is converted to
420v by Metal. The encoder's original `CMSampleBufferRef` reaches the decoder
without rebuilding or copying its compressed payload. Decoder-owned NV12 planes
are mapped as `R8Unorm` and `RG8Unorm` textures without copying decoded pixels.
The fused effect creates one new 32BGRA output buffer because that is the stable
public output contract.

Decode callbacks return after committing Metal work. GPU completion uses
bounded steady/warm-up watchdog deadlines, and accepted submissions are
delivered in order even if command buffers finish out of order. Flush waits for
both codec contexts and outstanding Metal commands.

With `AUTO`, inspect all five runtime evidence flags:
`nv12_metal_fast_path`, `metal_texture_cache`, `fused_metal_effects`,
`asynchronous_metal_delivery`, and `ordered_delivery`. A successful prepare
alone does not prove that the fast path remained active.

### Validation

The deterministic Phase 6 runner renders MP4/report/log evidence for 36 effects,
three codecs, and two resolutions. It feeds FFmpeg raw NV12 directly into 420v
pixel buffers. Its hard gates are direct 420v input, at least 20 processing fps,
p95 at or below 50ms, preserved frame count, hardware encode/decode, zero GPU
timeout, and zero unintended fallback/error/drop.

On the Apple M5 Max validation run dated 2026-07-25, all 216 cells passed. The
slowest cell was HEVC 1920×1080 `generation_cascade` at 52.043 processing fps
and 20.346ms p95. Treat this as machine/input evidence, not a guarantee for a
different Mac or an integrated host.

Run:

```bash
python3 scripts/validate_videotoolbox_fast_path.py input.mov \
  --filter-bin build/glic_codec_glitch_filter \
  --output-dir validation/videotoolbox-fast-path
```

Keep `glic_realtime.metallib` in the application bundle, submit IOSurface-backed
420v directly when the capture pipeline provides it, treat backpressure as an
input drop rather than a reason to block, record the runtime path flags, and
flush before destroying the context. Re-measure the 20fps/50ms gate inside the
actual host with capture, presentation, and its other processing enabled.
`process_video.py --processing-mode codec_glitch` uses this direct NV12 input by
default. The standalone raw filter retains BGRA as its compatibility default;
pass `--input-pixel-format nv12 --pixel-path nv12` to request the direct path.
