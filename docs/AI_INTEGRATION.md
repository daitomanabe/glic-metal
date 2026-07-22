# GLIC Metal AI Integration Contract

[日本語](#日本語) | [English](#english)

This file is the first-read implementation contract for an AI coding agent
integrating GLIC Metal into another application. Do not infer behavior from
internal files when a public API or rule is specified here. The machine-readable
companion is `resources/integration-manifest.json`.

## 日本語

### 目的

別アプリへ、採用済み19プリセットを同じ名前・同じ値で組み込む。ホストアプリは
メニューに19件を表示し、選択されたカテゴリーに応じて同期画像処理または非同期
VideoToolbox処理へ振り分ける。

### 最初に読むファイル

1. `docs/AI_INTEGRATION.md` — AI実装契約（このファイル）
2. `resources/integration-manifest.json` — 機械可読な依存関係と制約
3. `include/glic_metal/glitch_presets.h` — 採用プリセットAPI
4. `include/glic_metal/glic_metal.h` — Original / Spatial画像API
5. `include/glic_metal/codec_glitch.h` — Codec非同期API
6. `docs/EMBEDDING.md` — 人間向けの詳細な組み込み手順

`src/` 内のヘッダーは公開APIではない。他アプリからincludeしない。

### 絶対条件

- 正式なランタイム値は `glitch_presets.h` のC APIから取得する。
- `selected-presets.json` は確認・交換用データであり、実行時に必須ではない。
- プリセット名をホスト側へ保存するときは `original__vv01` のような完全名を使う。
- `original` と `spatial` は `glic_metal_context` へ送る。
- `codec` は `glic_codec_glitch_context` へ送る。画像contextへ送らない。
- `prepare` はcapture/render callback内で呼ばない。
- 1つのcontextを複数の同時ストリームから呼ばない。
- 1ストリームにつき1本のserial processing queueを使う。
- public structは必ず対応する `_init()` 関数で初期化する。
- 内部C++ API、プリセット値、Metal shaderをホスト側へ複製しない。
- Processing版GLICとのpixel完全一致を主張しない。

### 採用バンク

| Category | Count | Execution | Input | Context |
|---|---:|---|---|---|
| `original` | 14 | 同期 | BGRA8/RGBA8 CPU buffer | `glic_metal_context` |
| `spatial` | 4 | 同期 | CPU bufferまたはBGRA8Unorm texture | `glic_metal_context` |
| `codec` | 1 | 非同期 | `CVPixelBufferRef` 32BGRA | `glic_codec_glitch_context` |

19件の順序と値は `glic_glitch_preset_count()` と
`glic_glitch_preset_get()` が返す。全144件を返す
`glic_metal_enumerate_presets()` は採用メニューには使用しない。

### 必須のルーティング

```c
glic_glitch_preset_descriptor preset;
glic_glitch_preset_descriptor_init(&preset);
if (glic_glitch_preset_find(saved_name, &preset) !=
    GLIC_GLITCH_PRESET_OK) {
  /* Unknown names fail closed. Keep the previous active preset. */
  return;
}

switch (preset.category) {
case GLIC_GLITCH_PRESET_ORIGINAL:
case GLIC_GLITCH_PRESET_SPATIAL:
  /* Run on a control/background serial queue, outside frame callbacks. */
  glic_metal_config_init(&image_config);
  image_config.width = width;
  image_config.height = height;
  image_config.preset_directory = presets_path;
  image_config.metal_library_path = metallib_path;
  if (glic_glitch_preset_apply_metal(saved_name, &image_config) !=
          GLIC_GLITCH_PRESET_OK ||
      glic_metal_prepare(image_context, &image_config) != GLIC_METAL_OK) {
    /* Keep or restore the previous prepared image context. */
    return;
  }
  active_lane = IMAGE_LANE;
  break;

case GLIC_GLITCH_PRESET_CODEC:
  if (glic_glitch_preset_apply_codec(saved_name, &codec_controls) !=
          GLIC_GLITCH_PRESET_OK ||
      glic_codec_glitch_set_controls(codec_context, &codec_controls) !=
          GLIC_CODEC_GLITCH_OK) {
    return;
  }
  active_lane = CODEC_LANE;
  break;
}
```

`image_context` と `codec_context` は別に所有する。Codec contextは使用前に
`glic_codec_glitch_prepare()` で一度準備する。プリセット変更時は、画面へ古いlaneの
遅延出力を表示しないようホスト側のgeneration IDを増やし、古い非同期出力を捨てる。

### フレーム処理

Original / SpatialのCPU buffer経路:

```c
glic_metal_status status = glic_metal_process_frame(
    image_context,
    input_bgra, input_bytes_per_row,
    output_bgra, output_bytes_per_row,
    GLIC_METAL_PIXEL_FORMAT_BGRA8,
    frame_index);
```

SpatialのMetal texture経路では `glic_metal_metal.h` をincludeし、ホストの
uncommitted `MTLCommandBuffer` へ `glic_metal_encode_texture_objects()` でencodeする。
contextあたり最大3 frame in flightとし、commit・同期・texture lifetimeはホストが
管理する。Originalはtexture APIを使用できない。

Codec経路:

```c
glic_codec_glitch_status status = glic_codec_glitch_submit_pixel_buffer(
    codec_context, (void *)input_pixel_buffer, frame_index,
    pts.value, pts.timescale);

if (status == GLIC_CODEC_GLITCH_BACKPRESSURE) {
  /* Drop this input frame. Do not block the capture callback. */
}

glic_codec_glitch_frame frame;
glic_codec_glitch_frame_init(&frame);
status = glic_codec_glitch_copy_latest_pixel_buffer(codec_context, &frame);
if (status == GLIC_CODEC_GLITCH_OK) {
  present((CVPixelBufferRef)frame.pixel_buffer);
  glic_codec_glitch_pixel_buffer_release(frame.pixel_buffer);
}
```

`NO_FRAME_AVAILABLE` は正常状態。取得成功したpixel bufferは必ず
`glic_codec_glitch_pixel_buffer_release()` で1回だけ解放する。

### 配布方法

推奨順序:

1. macOS/Xcodeアプリ: `scripts/build_macos_sdk.sh` が生成する
   `GlicMetal.xcframework` と `GlicMetalResources.bundle` を追加する。
2. CMakeアプリ: `add_subdirectory()` または `find_package(GlicMetal)` を使い、
   `GlicMetal::GlicMetal` をlinkする。
3. 手動static library linkは、上記2方式が使用できない場合だけにする。

macOSでは次をlinkする:

- `libc++.tbd`
- `Foundation.framework`
- `Metal.framework`
- `CoreImage.framework`
- `CoreGraphics.framework`
- `CoreMedia.framework`
- `CoreVideo.framework`
- `VideoToolbox.framework`

`GlicMetalResources.bundle/Contents/Resources` には以下が入る:

- `Presets/` — Original / Spatialに必須（Spatialは`default`を基礎設定に使う）
- `glic_realtime.metallib` — Original / Spatial Metalに必須
- `selected-presets.json` — 確認・交換用
- `integration-manifest.json` — AI向け機械可読仕様

### リソースパス

ホストはbundleから絶対パスを解決し、その文字列を `glic_metal_prepare()` が戻るまで
保持する。ライブラリはprepare中に値をコピーする。`preset_directory` は
`.../Resources/Presets`、`metal_library_path` は
`.../Resources/glic_realtime.metallib` を指す。

### リアルタイム合格条件

- 最低解像度: 960×540
- 最低処理速度: 20fps
- p95 frame latency: 50ms以下
- Codec: hardware encoder / decoderの両方が必須
- Codec: 非意図的fallback、codec error、watchdog recovery、backpressure、
  output queue dropを計測する

他アプリへ組み込んだ後は、そのアプリ自身のcapture、表示、他処理を含めて再計測する。
ライブラリ単体の認証値をホストアプリ全体の保証として扱わない。

### 完了条件

- メニューが19件で、14 / 4 / 1に分類される。
- 全項目がstable nameから検索できる。
- Original、Spatial、Codecから最低1件ずつ実フレームを処理できる。
- プリセット変更をframe callback外で行う。
- Codec出力ownershipとbackpressureを正しく処理する。
- 960×540で20fps以上、p95 50ms以下をホスト内で計測する。
- アプリ終了時にcodecをflushし、両contextをdestroyする。
- リポジトリ統合時は以下を実行してすべて成功させる。

```bash
python3 scripts/check_public_release.py --source .
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

## English

### Agent implementation rules

Integrate the adopted 19-preset bank through the public C ABI. Treat
`resources/integration-manifest.json` as the machine-readable contract and
`glic_glitch_preset_*` as the authoritative runtime catalog. The JSON preset
bank is an inspection/exchange copy; parsing it is optional.

Route categories exactly:

- `original`: synchronous `glic_metal_context`, CPU BGRA/RGBA frame API;
- `spatial`: synchronous `glic_metal_context`, CPU frame or BGRA8Unorm Metal
  texture API;
- `codec`: asynchronous `glic_codec_glitch_context`, 32BGRA
  `CVPixelBufferRef` submit/poll API.

Never use `glic_metal_enumerate_presets()` for the adopted menu because it
returns the complete 144-preset compatibility corpus. Enumerate with
`glic_glitch_preset_count()` and `glic_glitch_preset_get()`. Persist the full
stable name, including its category prefix.

Prepare or switch presets on a control/background serial queue. Do not prepare
inside capture or render callbacks. Keep an image context and codec context as
separate objects, one serial processing queue per stream, and use a host-side
generation ID to reject late asynchronous codec output after lane switches.

The CMake target is `GlicMetal::GlicMetal`; the Swift module is `GlicMetal`.
Prefer the generated XCFramework and resource bundle for Xcode hosts. Link all
frameworks listed in `resources/integration-manifest.json`. Resolve `Presets`
and `glic_realtime.metallib` from the resource bundle. Original and Spatial
require both because Spatial loads `default` as its base configuration; Codec
requires neither runtime file.

Initialize every public struct with its matching `_init()` function. Treat
unknown presets and category mismatches as fail-closed. A codec submit may
return `BACKPRESSURE`; drop that input rather than blocking. A codec poll may
return `NO_FRAME_AVAILABLE`; this is normal. Release every successfully polled
pixel buffer exactly once with `glic_codec_glitch_pixel_buffer_release()`.

Do not import headers from `src/`, duplicate preset constants, modify H.264 VCL
payloads, or claim pixel-exact equivalence with the Processing implementation.
Validate the integrated host at 960×540 or greater, 20fps or greater, and p95
frame latency at or below 50ms. Codec acceptance additionally requires hardware
encode/decode and zero unintended reliability failures.
