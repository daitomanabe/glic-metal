# Downstream App Quick Start

[日本語](#日本語) | [English](#english)

## 日本語

GLIC Metalを別アプリへ組み込む最短手順です。リアルタイム処理とoffline file処理は
安全境界が異なるため、同じ呼び出し経路へ混在させません。

### 1. macOS SDKを作る

```bash
scripts/build_macos_sdk.sh build/GlicMetalSDK
```

生成物:

- `GlicMetal.xcframework` — C ABI / Swift module
- `GlicMetalResources.bundle` — Metal library、preset、machine-readable catalog
- `Documentation/` — 人間・AI向けの自己完結した仕様書
- `Tools/` — offline codec処理と評価CLI
- `SHA256SUMS` — 配布物の整合性

Xcode targetへXCFrameworkとresource bundleを追加し、`README.md`に列挙されたApple
frameworkをlinkします。最初に`AI_INTEGRATION.md`、次に
`Documentation/EMBEDDING.md`を読みます。

### 2. CMake packageを使う

```cmake
find_package(GlicMetal 1 CONFIG REQUIRED)

add_executable(my_video_app main.mm)
target_link_libraries(my_video_app PRIVATE GlicMetal::GlicMetal)

glic_metal_copy_resources(
  TARGET my_video_app
  DESTINATION "$<TARGET_FILE_DIR:my_video_app>/Resources")
```

外部アプリは`src/`をincludeせず、`<glic_metal/*.h>`だけを使用します。preset名は
`glic_glitch_preset_count()` / `glic_glitch_preset_get()`から取得し、独自に複製
しません。

### 3. offline Toolsを使う

SDK:

```bash
python3 -m pip install -r GlicMetalSDK/Tools/requirements.txt

python3 GlicMetalSDK/Tools/process_codec_lab.py input.mov output.mp4 \
  --effect motion_vector_vortex --codec hevc

FFEDIT="$(python3 GlicMetalSDK/Tools/install_ffglitch_reference.py \
  --print-ffedit)"
python3 GlicMetalSDK/Tools/process_native_syntax_glitch.py input.mov direct.mp4 \
  --effect compressed_motion_vector_vortex --ffedit "$FFEDIT"

python3 GlicMetalSDK/Tools/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax --codec all --ffedit "$FFEDIT"
```

CMake install:

- `${GLIC_METAL_TOOLS_DIR}` — installed Python entrypoints
- `${GLIC_METAL_PYTHON_REQUIREMENTS}` — NumPy / OpenCV requirements

Packet、Syntax、structured、transport、metadata、AV1 / AV2 / VP9 / VVC /
Theora / Dirac処理はホストのcapture/render callbackから
呼びません。別processとして実行し、JSON reportを完了通知として扱います。
`codec-lab-effects.json`と`offline-codec-effects.json`にない名前はfail-closedします。

### 4. 組み込み完了条件

- Original / Spatial / Codecをpublic C ABIだけで処理できる
- resource pathをbundleまたはCMake変数から解決している
- Codec出力のownership、backpressure、終了時flushを処理している
- host全体で960×540・20fps以上、p95 50ms以下を再測定している
- offline Toolsを別processで起動し、exit statusとJSONを検証している

完全な制約は`AI_INTEGRATION.md`と`integration-manifest.json`が正規仕様です。
全追加系統と実動画評価は`Documentation/GLITCH_EXPANSION.md`にあります。

## English

Use the generated SDK as the shortest downstream integration path:

```bash
scripts/build_macos_sdk.sh build/GlicMetalSDK
```

Add `GlicMetal.xcframework` and `GlicMetalResources.bundle` to the Xcode target.
Read `AI_INTEGRATION.md` first, then `Documentation/EMBEDDING.md`. Include only
the public `<glic_metal/*.h>` headers and enumerate adopted preset names through
the public C API.

For CMake consumers:

```cmake
find_package(GlicMetal 1 CONFIG REQUIRED)
target_link_libraries(my_video_app PRIVATE GlicMetal::GlicMetal)
glic_metal_copy_resources(
  TARGET my_video_app
  DESTINATION "$<TARGET_FILE_DIR:my_video_app>/Resources")
```

The generated SDK also contains a self-contained `Tools/` directory:

```bash
python3 -m pip install -r GlicMetalSDK/Tools/requirements.txt
python3 GlicMetalSDK/Tools/process_codec_lab.py input.mov output.mp4 \
  --effect motion_vector_vortex --codec hevc

FFEDIT="$(python3 GlicMetalSDK/Tools/install_ffglitch_reference.py \
  --print-ffedit)"
python3 GlicMetalSDK/Tools/process_native_syntax_glitch.py input.mov direct.mp4 \
  --effect compressed_coefficient_sign_flip --amount 1.0 --ffedit "$FFEDIT"

python3 GlicMetalSDK/Tools/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax --codec all --ffedit "$FFEDIT"
```

Installed CMake packages expose `GLIC_METAL_TOOLS_DIR` and
`GLIC_METAL_PYTHON_REQUIREMENTS`. Run packet, syntax, structured, transport,
metadata, all offline codec generations, and evolutionary workflows as child
processes; never invoke them from a capture or render callback. Treat their
exit status and JSON report as the completion contract.

The direct compressed-syntax tool supports catalogued MPEG-2 MV/qDCT/qscale
and MPEG-4 Part 2 MV operations and invokes the separately installed GPL
FFglitch executable. H.264/HEVC direct requests fail closed. The adjacent
batch evaluator provides resumable, token-free actual-video difference and
diversity ranking.

`AI_INTEGRATION.md` and `integration-manifest.json` remain the normative
human-readable and machine-readable contracts. See
`Documentation/GLITCH_EXPANSION.md` for all added families and actual-video
validation.
