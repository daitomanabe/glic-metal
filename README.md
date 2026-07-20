# GLIC Metal - Realtime Glitch Image Processing

[日本語](#日本語) | [English](#english)

---

## 日本語

C++20とMetal ComputeによるGLIC (GLitch Image Codec) のリアルタイム映像処理実装です。`glic-cpp`を基礎に、1920×1080のライブ処理向けGPU経路、CPUチャンネル並列、再利用可能なメモリ構成を追加しています。

### クレジット / Credits

**このプロジェクトは [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC) のJava/Processing版をC++にポートしたものです。**

- **オリジナル**: [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC) (Java/Processing)
- **ドキュメント**: [GLIC Documentation](https://docs.google.com/document/d/1cdJvEmSKNAkzkU0dFUa-kb_QJB2ISQg-QfCqpHLFlck/edit) - GlitchCodec/GLICより
- **C++ポート**: このリポジトリ

オリジナルのProcessing版から完全にポートし、さらに新しいグリッチ効果を追加しています。

### 特徴

- Processing版の全機能をC++20で再実装
- モダンC++機能を活用（`std::ranges`, `std::span`, `std::bit_cast`, `[[likely]]`属性など）
- クロスプラットフォーム対応 (macOS, Linux, Windows)
- コマンドラインインターフェース
- **144種類のプリセット対応** (オリジナルGLICのプリセットファイルを読み込み)
- 24種類の予測アルゴリズム（8種類追加）
- 6種類のエンコーディング方式（3種類追加）
- 6種類のポストプロセッシングエフェクト（新機能）
- テスト用画像付属（`daito-testimage.png`）

### ビルド方法

```bash
mkdir build && cd build
cmake ..
cmake --build .
```

### 使用方法

```bash
# エンコード（画像 → GLIC形式）
./glic encode input.png output.glic [options]

# デコード（GLIC形式 → 画像）
./glic decode input.glic output.png [options]

# プリセット一覧を表示
./glic --list-presets

# プリセットを使用してエンコード
./glic encode input.png output.glic --preset colour_waves
```

### リアルタイム処理（CPU / Metal）

`glic_realtime_bench` は `.glic` への直列化と再デコードを省き、presetの予測・量子化・変換特性をフレームへ直接適用します。ファイルcodecは互換性維持のため従来どおり利用できますが、リアルタイム出力は視覚表現を優先した専用経路であり、従来decodeとのピクセル完全一致は保証しません。

```bash
# Metalで960×540を120フレーム計測し、30fpsの平均+p95ゲートを確認
./build/glic_realtime_bench input-960x540.png \
  --preset bi0g4n1c --backend metal \
  --strength 1.0 \
  --frames 120 --warmup 10 --require-fps 30 \
  --output realtime-output.png --json realtime-report.json

# 全144 presetを検証
./build/glic_realtime_bench input-1920x1080.png \
  --all-presets --backend metal --frames 20 --warmup 3

# 3本の永続workerを使うCPU fallback
./build/glic_realtime_bench input-1920x1080.png \
  --preset default --backend cpu
```

リアルタイムAPIは [src/realtime.hpp](src/realtime.hpp) にあります。CPU backendは3チャンネルを永続workerで並列処理し、解像度変更時以外はworkspaceを再利用します。Metal backendはCPU配列を扱う同期APIに加え、`MTLTexture`を直接渡すゼロコピーAPIと、呼び出し側の`MTLCommandBuffer`へ処理を追加する非同期APIを提供します。

リアルタイム経路はpresetの予測・量子化・wavelet・encoding設定から、残差欠落、ブロック転送、RLEストリーク、RGB分離、ビットプレーン破損を生成します。破損パターンは数フレーム保持されるため、動画上で構造として認識できます。`--strength` は `0`（無加工）から `2`（最大）で、既定値は `1` です。

macOSでMetal shaderをビルドする際はFull Xcodeが必要です。CMakeはデフォルトで `/Applications/Xcode.app/Contents/Developer` を使用するため、システムの`xcode-select`設定を変更する必要はありません。

### 動画処理

`process_video.py` はFFmpegで動画をBGRAフレームへデコードし、1つのリアルタイムbackendを全フレームで再利用します。処理後は元動画の音声を戻し、H.264 MP4とJSON性能レポートを出力します。

```bash
python3 scripts/process_video.py input.mov output.mp4 \
  --preset bi0g4n1c --backend metal --strength 1.0
```

入力・出力動画をローカルに保持する場合は、Git対象外の `test-videos/` を使用できます。

### グリッチ差分QA

動画が正常に再生できることと、グリッチが十分に見えることは別々に検証します。`--passthrough` で同じBGRA・H.264経路の無加工対照を作り、`evaluate_effect_difference.py` で対応フレームの画素差、DeltaE76、SSIM、エッジ差を測定します。

```bash
python3 scripts/process_video.py input.mov control.mp4 \
  --passthrough --overwrite

python3 tools/evaluate_effect_difference.py input.mov \
  --control control.mp4 \
  --candidate glitch=output.mp4 \
  --output-json effect-difference.json \
  --output-md effect-difference.md \
  --heatmap effect-difference.png
```

評価ツールには `requirements-qa.txt` のNumPyとOpenCVが必要です。`VISIBLE` または `STRONG` のみを、視覚的に意味のあるグリッチとして合格にします。

### 無人preset探索

`glic_realtime_search` は外部APIやLLMを呼ばず、決定的なMAP-Elites探索で技術的に異なるpreset候補を収集します。128候補のランダム初期集団後は、2/3をarchive内eliteの変異、1/3をglobal random restartとし、変異にもchannel全再生成・channel交換・3 channel macro restartを含めます。これにより少数eliteの近傍へ探索が収束するのを防ぎます。複数入力・2 seed・8 frame phaseでMetal出力を評価し、無変化、過剰破壊、クリッピング、入力非依存ノイズを除外します。archiveへ入る可能性がある候補だけを、別の永続Metal backendで960×540・10 frame warm-up・120 frame計測し、wall-clockの平均とp95が両方33.333ms以下の場合に限ってeliteとして保存します。レシピだけでなく全評価frameと代表PNGもhash化するため、異なる設定から生じる同一出力がarchive枠を消費しません。

探索archiveはさらに、技術ゲート、9つの独立評価family、量子化Pareto front、知覚的近似画像cluster、cell/recipe-family制約付きMMRの順に絞り込めます。ランキングは同じarchive snapshotを960×540で再認証し、Metal、warm-up 10以上、計測120 frame以上、平均・p95とも30fps以上という証明が欠ける候補をfail-closedで除外します。速度差は認証済みp95 FPSを30〜60fpsで評価し、60fps以上で飽和させます。出力はTop 12、Shortlist 32、Reserve 64と全候補です。

画像解析は外部の `visual-liveliness` を毎回self-testして明暗・占有率・blob形状を測り、repo内の知覚特徴器がpHash/dHash、HSV分布、色、edge、block境界を測ります。代表PNGは静止画なので、`temporal_residual_delta` は時間変化量であって光学flowではありません。美的な「最適」を断定するものではなく、目視する候補を説明可能な少数へ絞る仕組みです。Full HDを線形1/4へ縮小済みの480×270入力では、追加縮小を避けるため `--scale 1` を指定します。

```bash
./build/glic_realtime_search \
  --input test-videos/search-inputs/video_00.png \
  --input test-videos/search-inputs/video_01.png \
  --output-dir search-runs/pilot \
  --duration-seconds 18000 --backend metal --scale 1

scripts/build_ranked_catalog.sh search-runs/pilot
```

`ranking.html`、全監査用の `ranking.json` / `ranking.csv`、Top 32の `shortlist.json`、Top 64の `selection.json` が生成されます。`performance-certifications.json` はarchive SHA、レシピSHA、認証binary・Metal shader library・入力・ハードウェアidentityと、各候補のmean/p95/p99/maxを保持します。未認証、CPU、誤解像度、120 frame未満、平均またはp95が33.333msを超える候補でTop枠を補完しません。`generation-directives.json` には過密な知覚cluster、類似出力が多いrecipe family、unique clusterが少ないarchive cellを保存します。画像解析と性能認証はcacheされ、archive世代不一致時には部分結果を公開せず直前のatomic reportを保持します。

5時間の無人実行は、API credentialを子プロセスへ渡さず、`caffeinate`、空き容量監視、二重起動防止、signal checkpointを行うsupervisorから起動できます。ランキングは既定で5分ごとに更新され、終了後に安定したarchiveから最終生成されます。`status.json`、`supervisor-status.json`、`archive.json`、`candidates.ndjson` が進捗と復旧元です。同一入力・seed・backendだけが `--resume` でき、内容不一致は拒否されます。

```bash
SEARCH_BIN="$PWD/build/glic_realtime_search" \
SEARCH_OUTPUT_DIR="$PWD/search-runs/unattended" \
SEARCH_DURATION_SECONDS=18000 SEARCH_BACKEND=metal MIN_FREE_GIB=45 \
SEARCH_INPUT_ARGS='--input test-videos/search-inputs/video_00.png --input test-videos/search-inputs/video_01.png --scale 1' \
scripts/run_search_supervisor.sh
```

### プリセットオプション

| オプション | 説明 |
|-----------|------|
| `--preset <name>` | プリセット名を指定（例: `default`, `colour_waves`, `cubism`） |
| `--presets-dir <path>` | プリセットディレクトリを指定（デフォルト: `./presets`） |
| `--list-presets` | 利用可能なプリセット一覧を表示 |

### エンコードオプション

| オプション | デフォルト | 説明 |
|-----------|----------|------|
| `--colorspace <name>` | HWB | 色空間 |
| `--min-block <size>` | 2 | 最小ブロックサイズ |
| `--max-block <size>` | 256 | 最大ブロックサイズ |
| `--threshold <value>` | 15 | セグメンテーション閾値 |
| `--prediction <method>` | PAETH | 予測方式 |
| `--quantization <value>` | 110 | 量子化値 (0-255) |
| `--clamp <method>` | none | クランプ方式 (none, mod256) |
| `--wavelet <name>` | SYMLET8 | ウェーブレット |
| `--transform <type>` | fwt | 変換タイプ (fwt, wpt) |
| `--scale <value>` | 20 | 変換スケール |
| `--encoding <method>` | packed | エンコード方式 |
| `--border <r,g,b>` | 128,128,128 | 境界色 (RGB) |

### デコードオプション（ポストエフェクト）

| オプション | デフォルト | 説明 |
|-----------|----------|------|
| `--effect <name>` | - | エフェクト適用（複数指定可） |
| `--effect-intensity <n>` | 50 | エフェクト強度 (0-100) |
| `--effect-blocksize <n>` | 8 | ブロックサイズ (pixelate, glitch用) |
| `--effect-offset <x,y>` | 2,0 | 色収差オフセット |
| `--effect-levels <n>` | 4 | ポスタライズレベル数 |

### 使用例

```bash
# 基本的なエンコード・デコード
./glic encode photo.png glitched.glic
./glic decode glitched.glic result.png

# プリセットを使用（推奨）
./glic encode photo.png out.glic --preset colour_waves
./glic encode photo.png out.glic --preset cubism
./glic encode photo.png out.glic --preset 8-b1tz
./glic encode photo.png out.glic --preset bl33dyl1n3z

# スパイラル予測（渦巻き状のアーティファクト）
./glic encode photo.png out.glic --prediction SPIRAL --quantization 180

# 波形予測 + YUV色空間
./glic encode photo.png out.glic --colorspace YUV --prediction WAVE

# ポストエフェクトを適用
./glic decode out.glic result.png --effect scanline --effect chromatic

# 複数エフェクトの組み合わせ
./glic decode out.glic result.png --effect posterize --effect-levels 4 --effect glitch
```

### サンプルスクリプト

`examples/` ディレクトリにサンプルスクリプトがあります：

#### quick_start.sh

クイックスタートガイドを表示します。利用可能なプリセットの一覧と基本的なコマンド例を確認できます。

```bash
./examples/quick_start.sh
```

**出力例：**
```
============================================
GLIC Quick Start
============================================

1. List available presets:
   $ ./build/glic --list-presets

  8-b1tz
  abstract_expressionism
  bl33dyl1n3z
  blocks
  ...

2. Show help:
   $ ./build/glic --help

3. Example commands:

   # Basic encode/decode
   ./build/glic encode input.png output.glic
   ./build/glic decode output.glic result.png

   # Encode with preset
   ./build/glic encode input.png output.glic --preset colour_waves
```

#### test_presets.sh

複数のプリセットを一括でテストし、出力ファイルを生成します。

```bash
./examples/test_presets.sh input.png
```

**テストされるプリセット：**
- `default` - デフォルト設定
- `colour_waves` - カラーウェーブ効果
- `cubism` - キュビズム風
- `8-b1tz` - 8ビット風グリッチ
- `bl33dyl1n3z` - ブリーディングライン
- `high_compression` - 高圧縮
- `abstract_expressionism` - 抽象表現主義風
- `blocks` - ブロック状
- `scanlined` - スキャンライン
- `webp` - WebP風圧縮

**出力例：**
```
============================================
GLIC Preset Test
============================================
Input: photo.png
Output directory: examples/output

--------------------------------------------
Testing preset: colour_waves
--------------------------------------------
  Encoding...
  Decoding...
  Output: examples/output/photo_colour_waves.png (245K)
```

#### 付属のテスト画像を使う

リポジトリにはテスト用画像 `daito-testimage.png` が含まれています：

```bash
# テスト画像でプリセットをテスト
./examples/test_presets.sh daito-testimage.png

# 直接エンコード
./build/glic encode daito-testimage.png output.glic --preset cubism
./build/glic decode output.glic result.png
```

### 機能一覧

#### 色空間 (16種類)
RGB, HSB, HWB, OHTA, CMY, XYZ, YXY, LAB, LUV, HCL, YUV, YPbPr, YCbCr, YDbDr, GS, R-GGB-G

#### 予測アルゴリズム (24種類)

**基本予測 (16種類 - オリジナルGLIC):** NONE, CORNER, H, V, DC, DCMEDIAN, MEDIAN, AVG, TRUEMOTION, PAETH, LDIAG, HV, JPEGLS, DIFF, REF, ANGLE

**C++版で追加 (8種類):**
| 名前 | 説明 |
|------|------|
| SPIRAL | 中心からスパイラル状に予測 |
| NOISE | 位置ハッシュベースのノイズ |
| GRADIENT | 4コーナーからバイリニア補間 |
| MIRROR | ミラー/反転予測 |
| WAVE | 正弦波ベースの変位 |
| CHECKERBOARD | 市松模様で交互予測 |
| RADIAL | 中心からの放射状グラデーション |
| EDGE | エッジ検出ベースの予測 |

**メタ予測:** SAD, BSAD, RANDOM

#### エンコード方式 (6種類)

**基本 (3種類 - オリジナルGLIC):** raw, packed, rle

**C++版で追加 (3種類):** delta, xor, zigzag

#### ポストエフェクト (6種類) - C++版新機能

| 名前 | 説明 |
|------|------|
| pixelate | ピクセル化（モザイク効果） |
| scanline | スキャンライン（CRTモニター風） |
| chromatic | 色収差（RGBチャンネルオフセット） |
| dither | ディザリング（Bayerパターン） |
| posterize | ポスタライズ（色数削減） |
| glitch | グリッチシフト（ランダムな行ずれ） |

#### ウェーブレット変換
Haar, Daubechies (DB2-DB10), Symlet (SYM2-SYM10), Coiflet (COIF1-COIF5)

### 依存関係

- C++20 以上（clang 13+, gcc 10+, MSVC 19.29+）
- CMake 3.16 以上
- stb_image / stb_image_write（gitサブモジュールとして含まれています）
- macOSでMetalリアルタイムbackendをビルドする場合はFull Xcode

### ブランチ

- `main` - glic-metal開発ブランチ

---

## English

A real-time GLIC (GLitch Image Codec) implementation using C++20 and Metal Compute. Built on `glic-cpp`, it adds a GPU path for live 1920×1080 processing, CPU channel parallelism, and reusable memory workspaces.

### Credits

**This project is a C++ port of the Java/Processing version of [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC).**

- **Original**: [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC) (Java/Processing)
- **Documentation**: [GLIC Documentation](https://docs.google.com/document/d/1cdJvEmSKNAkzkU0dFUa-kb_QJB2ISQg-QfCqpHLFlck/edit) - From GlitchCodec/GLIC
- **C++ Port**: This repository

This is a complete port from the original Processing version with additional glitch effects.

### Features

- Full reimplementation of Processing version in C++20
- Modern C++ features (`std::ranges`, `std::span`, `std::bit_cast`, `[[likely]]` attributes, etc.)
- Cross-platform support (macOS, Linux, Windows)
- Command-line interface
- **144 presets supported** (loads original GLIC preset files)
- 24 prediction algorithms (+8 new)
- 6 encoding methods (+3 new)
- 6 post-processing effects (new feature)
- Test image included (`daito-testimage.png`)

### Build

```bash
mkdir build && cd build
cmake ..
cmake --build .
```

### Usage

```bash
# Encode (image → GLIC format)
./glic encode input.png output.glic [options]

# Decode (GLIC format → image)
./glic decode input.glic output.png [options]

# List available presets
./glic --list-presets

# Encode with preset
./glic encode input.png output.glic --preset colour_waves
```

### Realtime Processing (CPU / Metal)

`glic_realtime_bench` bypasses `.glic` serialization and immediate decoding, applying the preset's prediction, quantization, and transform character directly to each frame. The compatible file codec remains available. Realtime output is a visual path and is not guaranteed to be pixel-identical to the legacy decoder.

```bash
# Benchmark 120 960x540 frames on Metal with a 30 fps mean+p95 gate
./build/glic_realtime_bench input-960x540.png \
  --preset bi0g4n1c --backend metal \
  --strength 1.0 \
  --frames 120 --warmup 10 --require-fps 30 \
  --output realtime-output.png --json realtime-report.json

# Validate all 144 presets
./build/glic_realtime_bench input-1920x1080.png \
  --all-presets --backend metal --frames 20 --warmup 3

# CPU fallback with three persistent channel workers
./build/glic_realtime_bench input-1920x1080.png \
  --preset default --backend cpu
```

The realtime API is declared in [src/realtime.hpp](src/realtime.hpp). The CPU backend reuses resolution-sized workspaces after preparation. The Metal backend provides a synchronous CPU-buffer API, an opaque zero-copy `MTLTexture` API, and a non-blocking API that appends work to the caller's `MTLCommandBuffer`.

The realtime path derives residual loss, block displacement, RLE streaks, RGB separation, and bit-plane damage from each preset's prediction, quantization, wavelet, and encoding settings. Corruption patterns are held for several frames so they read as temporal structures. `--strength` ranges from `0` (off) to `2` (maximum) and defaults to `1`.

Full Xcode is required to compile the Metal shader on macOS. CMake uses `/Applications/Xcode.app/Contents/Developer` by default, so it does not need to change the system `xcode-select` setting.

### Video processing

`process_video.py` uses FFmpeg to decode a video into BGRA frames and reuses one realtime backend across the complete stream. It restores the source audio after processing and writes both an H.264 MP4 and a JSON performance report.

```bash
python3 scripts/process_video.py input.mov output.mp4 \
  --preset bi0g4n1c --backend metal --strength 1.0
```

Use the Git-ignored `test-videos/` directory for local input and preview files.

### Glitch difference QA

A technically valid video and a visibly glitched video are separate validation targets. Create an unchanged control through the same BGRA and H.264 path with `--passthrough`, then use `evaluate_effect_difference.py` to measure aligned-frame pixel differences, DeltaE76, SSIM, and edge disagreement.

```bash
python3 scripts/process_video.py input.mov control.mp4 \
  --passthrough --overwrite

python3 tools/evaluate_effect_difference.py input.mov \
  --control control.mp4 \
  --candidate glitch=output.mp4 \
  --output-json effect-difference.json \
  --output-md effect-difference.md \
  --heatmap effect-difference.png
```

The evaluator requires NumPy and OpenCV from `requirements-qa.txt`. Only `VISIBLE` and `STRONG` pass the meaningful-glitch gate.

### Unattended preset search

`glic_realtime_search` is a deterministic, API-free MAP-Elites search for technically diverse preset candidates. After 128 random seeds, two candidates in three mutate archive elites and one in three performs a global random restart. Mutations also include full-channel regeneration, channel swaps, and a rare three-channel macro restart. After the low-resolution visual gates, only candidates that could enter the archive are certified on a separate persistent Metal backend at 960x540 with 10 warm-up and 120 measured frames. A candidate becomes an elite only when both mean and p95 synchronous wall time are at most 33.333 ms.

The archive is narrowed in stages: technical gates, nine separate score families, quantized Pareto fronts, perceptual near-duplicate clusters, and MMR selection with cell/recipe-family quotas. The ranking pipeline independently certifies the exact archive snapshot and fails closed unless every row has a matching recipe identity and Metal 960x540 measurement. Missing certification, CPU results, fewer than 120 frames, or mean/p95 above the 30 fps budget are excluded and never used to fill Top 12/32/64. Certified p95 FPS is scored from 30 to 60 fps and saturated above 60.

An external, self-tested `visual-liveliness` instrument measures presence and connected-component shape. The repository analyzer adds pHash/dHash, HSV, color, edge, and block-boundary descriptors. A representative PNG is still only one frame, so `temporal_residual_delta` is labelled activity rather than optical flow. This is deterministic technical/perceptual triage, not a claim of learned aesthetic optimality. Inputs already reduced from Full HD to 480×270 should use `--scale 1`.

```bash
./build/glic_realtime_search \
  --input test-videos/search-inputs/video_00.png \
  --input test-videos/search-inputs/video_01.png \
  --output-dir search-runs/pilot \
  --duration-seconds 18000 --backend metal --scale 1

scripts/build_ranked_catalog.sh search-runs/pilot
```

The pipeline writes `ranking.html`, full-audit `ranking.json` / `ranking.csv`, Top-32 `shortlist.json`, Top-64 `selection.json`, and `performance-certifications.json`. The certification sidecar records archive/recipe identities, certifier binary, Metal shader library, input and hardware identities, plus mean/p95/p99/max timings for every elite. Performance and image-analysis caches only reuse matching identities. Missing rows, failed self-tests, or mismatched archive generations fail closed and preserve the previous atomic reports.

For a five-hour unattended run, `run_search_supervisor.sh` adds `caffeinate`, disk-space checks, an atomic lock, credential-free child environment, logs, graceful checkpoints, five-minute ranking snapshots, and a final stable ranking. Resume is refused when the input content, seed, scale, or resolved backend differs from `run-config.json`.

```bash
SEARCH_BIN="$PWD/build/glic_realtime_search" \
SEARCH_OUTPUT_DIR="$PWD/search-runs/unattended" \
SEARCH_DURATION_SECONDS=18000 SEARCH_BACKEND=metal MIN_FREE_GIB=45 \
SEARCH_INPUT_ARGS='--input test-videos/search-inputs/video_00.png --input test-videos/search-inputs/video_01.png --scale 1' \
scripts/run_search_supervisor.sh
```

### Preset Options

| Option | Description |
|--------|-------------|
| `--preset <name>` | Preset name (e.g., `default`, `colour_waves`, `cubism`) |
| `--presets-dir <path>` | Presets directory (default: `./presets`) |
| `--list-presets` | List all available presets |

### Encode Options

| Option | Default | Description |
|--------|---------|-------------|
| `--colorspace <name>` | HWB | Color space |
| `--min-block <size>` | 2 | Minimum block size |
| `--max-block <size>` | 256 | Maximum block size |
| `--threshold <value>` | 15 | Segmentation threshold |
| `--prediction <method>` | PAETH | Prediction method |
| `--quantization <value>` | 110 | Quantization value (0-255) |
| `--clamp <method>` | none | Clamp method (none, mod256) |
| `--wavelet <name>` | SYMLET8 | Wavelet type |
| `--transform <type>` | fwt | Transform type (fwt, wpt) |
| `--scale <value>` | 20 | Transform scale |
| `--encoding <method>` | packed | Encoding method |
| `--border <r,g,b>` | 128,128,128 | Border color (RGB) |

### Decode Options (Post Effects)

| Option | Default | Description |
|--------|---------|-------------|
| `--effect <name>` | - | Apply effect (can be used multiple times) |
| `--effect-intensity <n>` | 50 | Effect intensity (0-100) |
| `--effect-blocksize <n>` | 8 | Block size (for pixelate, glitch) |
| `--effect-offset <x,y>` | 2,0 | Chromatic aberration offset |
| `--effect-levels <n>` | 4 | Posterize levels |

### Examples

```bash
# Basic encode/decode
./glic encode photo.png glitched.glic
./glic decode glitched.glic result.png

# Using presets (recommended)
./glic encode photo.png out.glic --preset colour_waves
./glic encode photo.png out.glic --preset cubism
./glic encode photo.png out.glic --preset 8-b1tz
./glic encode photo.png out.glic --preset bl33dyl1n3z

# Spiral prediction (spiral artifacts)
./glic encode photo.png out.glic --prediction SPIRAL --quantization 180

# Wave prediction + YUV color space
./glic encode photo.png out.glic --colorspace YUV --prediction WAVE

# Apply post effects
./glic decode out.glic result.png --effect scanline --effect chromatic

# Combine multiple effects
./glic decode out.glic result.png --effect posterize --effect-levels 4 --effect glitch
```

### Example Scripts

Sample scripts are available in the `examples/` directory:

#### quick_start.sh

Displays a quick start guide with available presets and example commands.

```bash
./examples/quick_start.sh
```

#### test_presets.sh

Tests multiple presets at once and generates output files.

```bash
./examples/test_presets.sh input.png
```

**Tested presets:** default, colour_waves, cubism, 8-b1tz, bl33dyl1n3z, high_compression, abstract_expressionism, blocks, scanlined, webp

#### Using the included test image

The repository includes a test image `daito-testimage.png`:

```bash
# Test presets with the included image
./examples/test_presets.sh daito-testimage.png

# Direct encode
./build/glic encode daito-testimage.png output.glic --preset cubism
./build/glic decode output.glic result.png
```

### Feature List

#### Color Spaces (16 types)
RGB, HSB, HWB, OHTA, CMY, XYZ, YXY, LAB, LUV, HCL, YUV, YPbPr, YCbCr, YDbDr, GS, R-GGB-G

#### Prediction Algorithms (24 types)

**Basic (16 types - Original GLIC):** NONE, CORNER, H, V, DC, DCMEDIAN, MEDIAN, AVG, TRUEMOTION, PAETH, LDIAG, HV, JPEGLS, DIFF, REF, ANGLE

**Added in C++ version (8 types):**
| Name | Description |
|------|-------------|
| SPIRAL | Spiral prediction from center |
| NOISE | Position hash-based noise |
| GRADIENT | Bilinear interpolation from 4 corners |
| MIRROR | Mirror/flip prediction |
| WAVE | Sine wave-based displacement |
| CHECKERBOARD | Alternating checkerboard prediction |
| RADIAL | Radial gradient from center |
| EDGE | Edge detection-based prediction |

**Meta predictions:** SAD, BSAD, RANDOM

#### Encoding Methods (6 types)

**Basic (3 types - Original GLIC):** raw, packed, rle

**Added in C++ version (3 types):** delta, xor, zigzag

#### Post Effects (6 types) - New in C++ version

| Name | Description |
|------|-------------|
| pixelate | Pixelation (mosaic effect) |
| scanline | Scanlines (CRT monitor style) |
| chromatic | Chromatic aberration (RGB channel offset) |
| dither | Dithering (Bayer pattern) |
| posterize | Posterize (reduce color levels) |
| glitch | Glitch shift (random row displacement) |

#### Wavelet Transforms
Haar, Daubechies (DB2-DB10), Symlet (SYM2-SYM10), Coiflet (COIF1-COIF5)

### Dependencies

- C++20 or later (clang 13+, gcc 10+, MSVC 19.29+)
- CMake 3.16 or later
- stb_image / stb_image_write (included as git submodule)
- Full Xcode on macOS when building the Metal realtime backend

### Branches

- `main` - glic-metal development branch

---

## License

MIT License

## Acknowledgments

This project would not be possible without:

- **[GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC)** - The original GLIC implementation in Java/Processing. This C++ version is a port of their work.
- **[GLIC Documentation](https://docs.google.com/document/d/1cdJvEmSKNAkzkU0dFUa-kb_QJB2ISQg-QfCqpHLFlck/edit)** - Original documentation from GlitchCodec/GLIC
- **[nothings/stb](https://github.com/nothings/stb)** - stb_image library for image I/O
