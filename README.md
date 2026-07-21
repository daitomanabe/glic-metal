# GLIC Metal - Realtime Glitch Image Processing

[日本語](#日本語) | [English](#english)

**Status: pre-release.** The source, tests, and macOS application are under
active development; no stable binary release has been tagged yet.

[Build guide](docs/BUILDING.md) ·
[Embedding guide](docs/EMBEDDING.md) ·
[Original-preset fidelity](docs/ORIGINAL_PRESET_REALTIME.md) ·
[Preset catalog](docs/original-preset-catalog.md) ·
[Repository structure](FILE-STRUCTURE.md) ·
[Release checklist](docs/PUBLIC_RELEASE.md) ·
[Contributing](CONTRIBUTING.md)

---

## 日本語

C++20とMetal ComputeによるGLIC (GLitch Image Codec) のリアルタイム映像処理実装です。`glic-cpp`を基礎に、1920×1080のライブ処理向けGPU経路、CPUチャンネル並列、再利用可能なメモリ構成を追加しています。

### クレジット / Credits

**このプロジェクトは [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC) のJava/Processing版をC++にポートしたものです。**

- **オリジナル**: [GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC) (Java/Processing)
- **ドキュメント**: [GLIC Documentation](https://docs.google.com/document/d/1cdJvEmSKNAkzkU0dFUa-kb_QJB2ISQg-QfCqpHLFlck/edit) - GlitchCodec/GLICより
- **C++ポート**: このリポジトリ

オリジナルのパラメータ意味、ファイルcodec、リアルタイム近似、原作スタイル再構成を、互換性レベルを明示して段階的に移植しています。リアルタイム合格をProcessing版とのピクセル完全一致とは扱いません。

### 特徴

- C++20によるファイルcodecと、CPU / Metalリアルタイム処理
- C / C++ / Objective-C / Swiftから利用できる安定C ABIとCMake package
- モダンC++機能を活用（`std::ranges`, `std::span`, `std::bit_cast`, `[[likely]]`属性など）
- CPU経路はmacOS / LinuxをCI対象とし、Windowsは設計対象・未認証
- コマンドラインインターフェース
- **上流144プリセットを意味変換・互換性分類**（SHA-256固定コーパス）
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
  --preset-semantics original \
  --strength 1.0 \
  --frames 120 --warmup 10 --require-fps 30 \
  --output realtime-output.png --json realtime-report.json

# 全144 presetを検証
./build/glic_realtime_bench input-960x540.png \
  --all-presets --backend metal --preset-semantics original \
  --frames 120 --warmup 10 --require-fps 30

# 原作スタイルの再構成を、対応する37 presetだけfail-closedで検証
./build/glic_original_realtime_bench input-960x540.png \
  --all-supported --presets-dir presets --backend metal --require-fps 30 \
  --json original-visual-report.json

# 3本の永続workerを使うCPU fallback
./build/glic_realtime_bench input-1920x1080.png \
  --preset default --backend cpu
```

リアルタイムAPIは [src/realtime.hpp](src/realtime.hpp) にあります。CPU backendは3チャンネルを永続workerで並列処理し、解像度変更時以外はworkspaceを再利用します。Metal backendはCPU配列を扱う同期APIに加え、`MTLTexture`を直接渡すゼロコピーAPIと、呼び出し側の`MTLCommandBuffer`へ処理を追加する非同期APIを提供します。

互換性レベル、上流GLICの20fps UI設定との違い、対応37 presetの境界は [docs/ORIGINAL_PRESET_REALTIME.md](docs/ORIGINAL_PRESET_REALTIME.md) にあります。2026-07-20のM4 Max隔離認証（commit `6e1d1f8`）では、`original_metal_visual` は960×540、warm-up 10 + 計測120フレームで、通常画像・uniform-noiseの双方とも37/37件が平均/p95 30fps gateを通過しました。CPU数値参照とは34/37件が規定範囲内、残り3件もエッジ方向・エッジ量・粗い構造による原作スタイル形態gateを通過しています。全144名を処理する `compat_realtime` は引き続き明示的に別の視覚近似です。

リアルタイム経路には、従来のブロック破損に加えて、走査線ティア、RGBチャンネルシア、アナログ同期崩れ、ミラーフォールド、輪郭エコー、ビットプレーン・ディザ、波形ワープ、ポスタライズ／ソラリゼーションの9機構があります。いずれも1 passのCPU/Metal実装で、フレームごとの確保を行いません。`--strength` は `0`（無加工）から `2`（最大）で、`--effect-amount`、`--effect-scale`、`--effect-rate` で機構固有の形状を制御します。

macOSでMetal shaderをビルドする際はFull Xcodeが必要です。CMakeはデフォルトで `/Applications/Xcode.app/Contents/Developer` を使用するため、システムの`xcode-select`設定を変更する必要はありません。

### Webカメラ・リアルタイムプレビュー

macOS版は`GLIC Webcam Preview.app`を生成します。内蔵または外付けカメラを
960×540・30fpsで取得し、`original_visual` Metal laneで処理します。画面上部の
ポップアップ、またはメニューバーの`Preset`から、対応済み37プリセットを実行中に
切り替えられます。`Quality`では原作準拠の`Strict`、fp32 CDF97と2フレーム分割木
再利用を使う`Fast Match`、画像解析allowlistから安全なpresetだけFastへ切り替える
`Auto 20fps`を選べます。Autoが画質gateを通らなかったpresetはStrictへ戻ります。
処理時間、GPU時間、処理fps、実効モード、drop数を画面上で確認できます。

キャプチャとMetal処理は別キューで、事前確保した3スロットから常に最新フレームを
選びます。古い待機フレームは破棄するため、重いpresetでも遅延が蓄積しません。
2026-07-21のM4 Max通常画像＋uniform-noise交差評価では19/37 presetがFast Matchの
局所SSIM、Lab色差、エッジ、クリップ、p95 50msの全gateを通過しました。

```bash
cmake --build build --target glic_webcam_preview --parallel
open "build/GLIC Webcam Preview.app"
```

初回起動時はmacOSのカメラ使用許可を承認してください。アプリは背面でもApp Napを
避けて処理を継続しますが、他のアプリを強制的に最前面へ移動しません。

`scripts/build_fast_match_allowlist.py`はStrict/Fastの複数ベンチマークとPNG群を
比較し、全入力ケースを通ったpresetだけを
`config/fast-match-allowlist.json`へ出力します。アプリはこのJSONをbundleへ同梱し、
欠落・不正時はfail-closedでAutoをStrictとして動かします。

### 動画処理

`process_video.py` はFFmpegで動画をBGRAフレームへデコードし、1つのリアルタイムbackendを全フレームで再利用します。処理後は元動画の音声を戻し、H.264 MP4とJSON性能レポートを出力します。

```bash
python3 scripts/process_video.py input.mov output.mp4 \
  --backend metal --effect-family line_tear \
  --effect-amount 0.9 --effect-scale 0.55 --effect-rate 0.4

# 原作スタイル対応presetを、明示的なoriginal_visualレーンで960x540/30fps処理
python3 scripts/process_video.py input.mov output-original.mp4 \
  --processing-mode original_visual --backend metal --preset burn \
  --width 960 --height 540 --fps 30 --overwrite
```

`original_visual` は `compat_realtime` と別の原作スタイル再構成レーンです。Metal backendはCPUで色空間を並列変換し、単一のProcessing互換48-bit RNGを原作のch0→ch1→ch2・DFS順で消費してquadtreeを作り、独立した3チャンネルの依存表だけを並列構築した後、Metalで予測・量子化・CDF97 FWT/WPT・逆変換を処理します。依存表はleaf境界と厳密に一致する`minBlockSize`グリッドで保持し、画素単位mapと同じfrontier順を小さいallocationと少ないclear/writeで生成します。adaptive varianceは最終分母でsplitが確定した時点から画像readとWelford更新だけを止め、残りのJava RNGを厳密にskip-aheadします。固定blockの分割／frontierは`prepare()`で一度だけ構築し、固定block preset、または32px超leafを許可しないadaptive presetは境界・matrix・scratchをthreadgroup memoryへ置きます。32px超leafを許可するadaptive CDF97 presetはframe間で安定したglobal workspace経路へ統一します。1フレームにつきcommand buffer 1回、完了待ち1回、mapped buffer copy 0回で、全workspaceとworkerを事前確保します。失敗フレームではsegmentation RNGを開始時点へrollbackするため、再試行やdrop後もtree列がずれません。MetalのCDF97積和は分割係数と補償加算でfloat-float精度を確保し、逆変換は有効な偶奇tapだけを同じ順序で走査します。WPTは全係数を更新するためmatrix/scratchをpassごとにping-pongし、FWTは既出の高周波bandを保つcopy-back経路を維持します。中間行列はfp32なのでCPU doubleとのpixel exactは主張しません。Processing丸め、生のplane shift/OR pack、早期判定と全サンプルoracleのleaf順・終端RNG一致をgolden testで固定し、CPU/Metal比較でも各presetの終端RNG、leaf順hash、省略量の完全一致を必須化します。未対応waveletやpredictor探索はfail-closedします。JSONの30fps判定は最初の10フレームを除いた最低120フレームについて、pipe待機・backpressureを含む平均とp95を対象にします。

探索結果の `ready_to_run_args` に含まれる `--canonical 'v2|...' --seed ...` を渡すと、preset名への変換を挟まず、評価した強度・機構・形状を動画上へ完全に再現できます。

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

`glic_realtime_search` は外部APIやLLMを呼ばず、決定的なMAP-Elites探索で技術的に異なる候補を収集します。recipe v2は9つの描画機構を均等に試し、長時間探索では同じ機構のeliteを親にして `amount`、`scale`、`rate`、強度を変異させます。定期的なglobal random restartも機構ごとの試行数を均等に保つため、1種類のブロック解像度へ収束しません。複数入力・2 seed・8 frame phaseでMetal出力を評価し、無変化、過剰破壊、クリッピング、入力非依存ノイズを除外します。archiveへ入る可能性がある候補だけを、別の永続Metal backendで960×540・10 frame warm-up・120 frame計測し、wall-clockの平均とp95が両方33.333ms以下の場合に限ってeliteとして保存します。recipe v1は従来互換の `legacy_block` として読み込めます。

探索archiveは、技術ゲート、独立評価family、量子化Pareto front、無加工との差分形状cluster、多様性制約の順に絞り込みます。画像解析は16:9を維持し、2/4/8/16/32/64/128pxの支配的な変化スケール、方向、差分領域、空間分布を測ります。Top 8は8種類の機構、4種類以上のスケール、1スケール最大2件、mega-scale最大1件、形状距離の下限を必須にします。条件を満たす組がなければ似た候補で穴埋めせず、`publishable=false` にします。ランキングは同じarchive snapshotを960×540で再認証し、Metal、warm-up 10以上、計測120 frame以上、平均・p95とも30fps以上という証明が欠ける候補をfail-closedで除外します。

画像解析は無加工フレームとの残差を主軸にし、色相差による見かけ上の多様性より、破損のスケール・方向・位置を重く評価します。代表PNGは静止画なので、`temporal_residual_delta` は時間変化量であって光学flowではありません。美的な「最適」を断定するものではなく、目視する候補を説明可能で似ていない少数へ絞る仕組みです。Full HDを線形1/4へ縮小済みの480×270入力では、追加縮小を避けるため `--scale 1` を指定します。

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

The port keeps the file codec, original parameter semantics, realtime visual approximation, and original-style reconstruction as explicitly separate compatibility levels. A realtime pass is not presented as Processing pixel equivalence.

### Features

- C++20 file codec plus CPU / Metal realtime processing
- Stable C ABI and installable CMake package for C, C++, Objective-C, and Swift
- Modern C++ features (`std::ranges`, `std::span`, `std::bit_cast`, `[[likely]]` attributes, etc.)
- CPU paths are CI-tested on macOS and Linux; Windows is designed for but not
  yet covered by the public CI matrix
- Command-line interface
- **All 144 upstream presets decoded and compatibility-classified** from a SHA-256-pinned corpus
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
./build/glic_realtime_bench input-960x540.png \
  --all-presets --backend metal --preset-semantics original \
  --frames 120 --warmup 10 --require-fps 30

# Fail-closed original-style reconstruction for the supported 37 presets
./build/glic_original_realtime_bench input-960x540.png \
  --all-supported --presets-dir presets --backend metal --require-fps 30 \
  --json original-visual-report.json

# CPU fallback with three persistent channel workers
./build/glic_realtime_bench input-1920x1080.png \
  --preset default --backend cpu
```

The realtime API is declared in [src/realtime.hpp](src/realtime.hpp). The CPU backend reuses resolution-sized workspaces after preparation. The Metal backend provides a synchronous CPU-buffer API, an opaque zero-copy `MTLTexture` API, and a non-blocking API that appends work to the caller's `MTLCommandBuffer`.

See [docs/ORIGINAL_PRESET_REALTIME.md](docs/ORIGINAL_PRESET_REALTIME.md) for compatibility levels and why upstream's 20 fps setting is a UI rate rather than codec throughput. In the previous isolated M4 Max certification (`6e1d1f8`, 2026-07-20), `original_metal_visual` passed the mean+p95 30 fps gate for all 37 supported presets on both the normal and uniform-noise inputs. Numeric CPU-reference comparison passed 34/37; the remaining three passed the separate blurred-structure and edge-morphology gate. The all-144 Metal path remains the separately labelled `compat_realtime` visual approximation.

The realtime path has nine explicit glitch mechanisms. `legacy_block` preserves the preset-derived codec damage path; the other eight are independent RGB mechanisms so selecting a legacy preset cannot collapse them back into the same block topology.

| Realtime family | Spatial character |
|---|---|
| `legacy_block` | Held macroblock displacement and codec damage |
| `line_tear` | Thin horizontal tears with long row displacement |
| `channel_shear` | Independently moving RGB channel bands |
| `analog_sync` | Raster wobble, vertical roll, jitter, and scanline loss |
| `mirror_fold` | Repeating mirrored ribbons |
| `edge_echo` | Directional displaced edge echoes |
| `bitplane_dither` | Ordered bit-plane XOR damage without resampling |
| `wave_warp` | Continuous two-axis waveform displacement |
| `poster_solar` | Animated posterization and solarization |

`--strength` ranges from `0` (off) to `2` (maximum). The explicit families also expose normalized `amount`, `scale`, and `rate` controls. Corruption patterns use a reproducible 32-bit seed and are held for several frames according to `rate`.

Full Xcode is required to compile the Metal shader on macOS. CMake uses `/Applications/Xcode.app/Contents/Developer` by default, so it does not need to change the system `xcode-select` setting.

### Realtime webcam preview

The macOS build produces `GLIC Webcam Preview.app`. It captures a built-in or
external camera at 960×540/30 fps and processes frames through the
`original_visual` Metal lane. Switch among all 37 supported presets from the
popup at the top of the window or the `Preset` application menu. The `Quality`
control selects original-compatible `Strict`, fp32 CDF97 plus two-frame tree
reuse in `Fast Match`, or fail-closed `Auto 20fps`. Auto uses Fast Match only
for presets in the analyzed allowlist and falls back to Strict otherwise.
Processed fps, total processing time, GPU time, effective mode, and dropped
frames remain visible.

Capture and Metal processing use separate queues. A preallocated three-slot
ring always selects the newest ready frame and discards stale queued frames, so
heavy presets do not accumulate latency. The 2026-07-21 M4 Max normal plus
uniform-noise cross-check admitted 19/37 presets after local SSIM, Lab color
difference, edge, clipping, and p95 50 ms gates.

```bash
cmake --build build --target glic_webcam_preview --parallel
open "build/GLIC Webcam Preview.app"
```

Approve camera access on first launch. The realtime loop opts out of App Nap
when the app is behind other software, without forcing its window to the front.
`scripts/build_fast_match_allowlist.py` intersects multiple paired Strict/Fast
benchmark and preview sets. CMake bundles the resulting
`config/fast-match-allowlist.json`; missing or invalid data makes Auto use
Strict.

### Video processing

`process_video.py` uses FFmpeg to decode a video into BGRA frames and reuses one realtime backend across the complete stream. It restores the source audio after processing and writes both an H.264 MP4 and a JSON performance report.

```bash
python3 scripts/process_video.py input.mov output.mp4 \
  --preset default --backend metal --strength 1.25 \
  --effect-family line_tear --effect-amount 0.9 \
  --effect-scale 0.35 --effect-rate 0.65 --seed 0x13579bdf

# Process a supported original-style preset at 960x540/30 fps
python3 scripts/process_video.py input.mov output-original.mp4 \
  --processing-mode original_visual --backend metal --preset burn \
  --width 960 --height 540 --fps 30 --overwrite
```

Run the complete fail-closed certification with explicit normal, uniform-noise,
and video inputs:

```bash
scripts/run_original_metal_validation.sh \
  --normal-image /absolute/path/to/dry-960x540.png \
  --noise-image /absolute/path/to/noise-960x540.png \
  --video /absolute/path/to/source-960x540-30fps.mkv \
  --output-dir /absolute/path/to/validation-results
```

This builds Release, runs CTest, requires 37/37 Metal presets to satisfy both
mean and p95 at 30 fps on normal and noise inputs, verifies CPU/Metal preview
provenance, segmentation RNG/leaf-order traces, and morphology, checks the real video pipeline at 960x540/30 fps,
requires a VISIBLE/STRONG dry/wet difference against a passthrough encode, and
requires a clean technical video-QA result. The final manifest refuses tracked
or untracked dirty source and hashes the benchmark/filter binaries plus the
compiled metallib. `--skip-video-qa` is available
only for an isolated machine without the global QA skill; the returned video
must then be checked separately before certification is complete.

`original_visual` is separate from `compat_realtime`. Its Metal backend parallelizes CPU colorspace conversion, consumes one Processing-compatible 48-bit RNG in upstream channel/DFS order to build the quadtree, then builds the three independent dependency grids concurrently before Metal reconstruction. Each grid uses exact `minBlockSize` cells because every leaf boundary is aligned to that unit, preserving frontier order while reducing allocation and per-frame clear/write work. Adaptive variance stops dead image reads and Welford updates once the final-denominator split is monotonic, while exact skip-ahead preserves the remaining Java RNG state. Fixed-block schedules are built once by `prepare()`; fixed-block presets and adaptive presets that do not admit leaves above 32 px keep boundaries, matrix, and scratch in threadgroup memory. Adaptive CDF97 presets whose bounds admit larger leaves use a frame-stable global workspace route. Each frame uses one command buffer, one completion wait, zero mapped-buffer copies, and preallocated workspaces. Failed frames roll the segmentation RNG back to their entry state, so retry/drop paths cannot shift later trees. Metal CDF97 uses split coefficients, compensated float-float accumulation, and the same ordered valid-tap subsequence while retaining fp32 matrix storage. Full-matrix WPT passes ping-pong matrix and scratch storage without per-pass copy-back; FWT retains copy-back to preserve earlier high-frequency bands. CPU-double pixel exactness is not claimed. Golden tests cover Processing rounding, raw plane packing, and ordered leaves plus terminal RNG state against an independent full-sampling oracle. CPU/Metal comparison additionally requires exact per-preset terminal RNG, ordered-leaf hash, and skipped-work counters. Unsupported wavelets and predictor-search modes fail during preflight. The JSON 30 fps gate covers 10 warm-up plus at least 120 measured frames, with both mean and p95 inside the frame budget.

Pass the exact `--canonical 'v2|...' --seed ...` values from a ranked row's
`ready_to_run_args` to reproduce the evaluated mechanism and controls without
converting the recipe back through a preset name.

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

`glic_realtime_search` is a deterministic, API-free MAP-Elites search for technically diverse preset candidates. Recipe v2 cycles all nine explicit mechanisms evenly. Long runs select parents from the same mechanism before mutating `amount`, `scale`, `rate`, and strength, while scheduled global restarts preserve equal trial counts. Recipe v1 remains readable as `legacy_block`. After the low-resolution visual gates, only candidates that could enter the mechanism-prefixed archive are certified on a separate persistent Metal backend at 960x540 with 10 warm-up and 120 measured frames. A candidate becomes an elite only when both mean and p95 synchronous wall time are at most 33.333 ms.

The archive is narrowed in stages: technical gates, score families, quantized Pareto fronts, dry/wet residual morphology clusters, and diversity selection. Analysis preserves 16:9 geometry and measures 2/4/8/16/32/64/128px artifact scales, orientation, residual coverage, and spatial grids. The Top 8 must cover eight mechanisms, at least four scale buckets, no more than two candidates per bucket, at most one mega-scale result, and a minimum morphology distance. If a feasible set does not exist the report is marked `publishable=false` instead of filling it with lookalikes. The ranking pipeline independently certifies the exact archive snapshot and fails closed unless every row has a matching recipe identity and Metal 960x540 measurement. Missing certification, CPU results, fewer than 120 frames, or mean/p95 above the 30 fps budget are excluded and never used to fill Top 12/32/64.

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

MIT License. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is an independently
maintained port and is not an official GlitchCodec release.

## Acknowledgments

This project would not be possible without:

- **[GlitchCodec/GLIC](https://github.com/GlitchCodec/GLIC)** - The original GLIC implementation in Java/Processing. This C++ version is a port of their work.
- **[GLIC Documentation](https://docs.google.com/document/d/1cdJvEmSKNAkzkU0dFUa-kb_QJB2ISQg-QfCqpHLFlck/edit)** - Original documentation from GlitchCodec/GLIC
- **[nothings/stb](https://github.com/nothings/stb)** - stb_image library for image I/O
