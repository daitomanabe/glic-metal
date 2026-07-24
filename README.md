# GLIC Metal - Realtime Glitch Image Processing

[日本語](#日本語) | [English](#english)

**Status: pre-release.** The source, tests, and macOS application are under
active development; no stable binary release has been tagged yet.

Canonical repository: <https://github.com/daitomanabe/glic-metal>

[Build guide](docs/BUILDING.md) ·
[Downstream quick start](docs/DOWNSTREAM_QUICKSTART.md) ·
[Embedding guide](docs/EMBEDDING.md) ·
[AI integration contract](docs/AI_INTEGRATION.md) ·
[Codec Glitch](docs/CODEC_GLITCH.md) ·
[Glitch expansion catalog](docs/GLITCH_EXPANSION.md) ·
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
- VideoToolbox hardware H.264 / HEVC / ProResとMetal-backed post pathによる36種類のCodec Glitch
- C / C++ / Objective-C / Swiftから利用できる安定C ABIとCMake package
- モダンC++機能を活用（`std::ranges`, `std::span`, `std::bit_cast`, `[[likely]]`属性など）
- CPU経路はmacOS / LinuxをCI対象とし、Windowsは設計対象・未認証
- コマンドラインインターフェース
- **上流144プリセットを意味変換・互換性分類**（SHA-256固定コーパス）
- 24種類の予測アルゴリズム（8種類追加）
- 6種類のエンコーディング方式（3種類追加）
- 6種類のポストプロセッシングエフェクト（新機能）
- テスト用画像付属（`daito-testimage.png`）

### Java/Processing版から、どのように高速化したか

オリジナルGLICは静止画像を対話的にencode / decodeするJava/Processing作品です。
[`GLIC.pde`](https://github.com/GlitchCodec/GLIC/blob/460e61bf9b01f7415cf973b3d655a0ae2c7962a7/GLIC.pde#L49-L124)
の`frameRate(20)`は画面を再描画する頻度であり、codecが毎秒20フレームを処理する
という意味ではありません。本実装はファイルcodecを残しつつ、ライブ映像用の処理経路を
別に設計しました。単純なJavaからC++への置換ではなく、処理の配置、並列単位、
メモリ寿命、フレーム配送を変更しています。

| 項目 | オリジナルJava/Processing版 | GLIC Metalのリアルタイム経路 |
|---|---|---|
| データ経路 | 静止画像を同期的にencode / decodeする操作 | `.glic`への直列化、entropy encoding、ファイルI/O、直後の再decodeを省き、メモリ上のフレームへ直接処理します |
| 演算場所 | codec処理をCPUで実行 | 予測、量子化、CDF 9/7 FWT/WPT、逆変換、空間グリッチをMetal computeへ移します |
| 並列化 | 原作の乱数消費順と再帰的なquadtree処理を重視 | 独立した3チャンネル、色変換slice、quadtreeの依存frontier内のleafを並列処理します |
| メモリ | 静止画処理に適した一時配列と変換行列 | plane、leaf記述子、変換scratch、Metal buffer、CPU workerを`prepare()`で確保し、通常のフレーム処理では再確保しません |
| 分割木 | 入力ごとにadaptive segmentationを評価 | 固定blockの分割木をcacheし、adaptive分散判定は結果が確定した時点で画像readを止めます。Fast Matchでは分割木を複数フレーム再利用できます |
| GPU投入 | 該当なし | 原作スタイル経路を1フレームあたり1 command buffer、1 completion waitにまとめ、Apple unified memoryではmapped buffer copyを行いません |
| ライブ配送 | Processingの同期UI | captureと処理を別queueにし、事前確保した3 slotから最新フレームを選びます。古い待機フレームを捨てるため遅延が蓄積しません |
| 動画codec glitch | 該当なし | VideoToolboxのhardware H.264 / HEVC / ProRes encode / decodeとMetal post pathを非同期・bounded queueで動かします |

原作スタイルの`Strict`経路では、Processing互換48-bit RNGの消費順、チャンネル順、
quadtreeのDFS順を変えると別の画像になるため、分割木の制御だけはCPUに残しています。
その後の独立した再構成をGPUへ渡し、小さいleafはthreadgroup memory、大きいadaptive
CDF97 leafは事前確保したglobal workspaceで処理します。固定分割は一度だけ構築し、
省略した乱数呼び出しはskip-aheadして終端RNG stateを一致させます。これにより、
原作の構造を保つ最適化と、見た目がほぼ同じであれば分割木も再利用する高速化を
明示的に選べます。

| 処理モード | 高速化と互換性の位置づけ |
|---|---|
| `original_visual` / Strict | 対応37 presetの原作スタイルを優先し、厳密な分割順とMetal再構成を使います |
| `original_visual` / Fast Match | fp32 CDF97とadaptive分割木の再利用を許容し、ライブ処理の余裕を増やします |
| `compat_realtime` | 全144 presetを1-pass Metal空間表現へ写像する、最も軽い視覚近似です |
| `codec_glitch` | 原作preset互換とは別に、hardware動画codecの時間方向の状態を利用します |

速度と忠実度は別々に検証しています。ベンチマークはwarm-up後の平均だけでなくp95、
drop、backpressureも評価し、画像側はCPU参照、leaf順hash、終端RNG state、SSIM、色差、
edge差を確認します。Metal CDF97の中間行列はfp32なので、CPU/JWave doubleとの
pixel完全一致は主張しません。対応範囲、計測条件、既知の差異は
[原作presetリアルタイム互換性](docs/ORIGINAL_PRESET_REALTIME.md)に記録しています。

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

他アプリ向けリアルタイムAPIは [include/glic_metal/glic_metal.h](include/glic_metal/glic_metal.h) にあります。AIエージェントは [AI向け組み込み仕様](docs/AI_INTEGRATION.md) に従ってください。CPU backendは3チャンネルを永続workerで並列処理し、解像度変更時以外はworkspaceを再利用します。Metal backendはCPU配列を扱う同期APIに加え、`MTLTexture`を直接渡すゼロコピーAPIと、呼び出し側の`MTLCommandBuffer`へ処理を追加する非同期APIを提供します。

互換性レベル、上流GLICの20fps UI設定との違い、対応37 presetの境界は [docs/ORIGINAL_PRESET_REALTIME.md](docs/ORIGINAL_PRESET_REALTIME.md) にあります。2026-07-20のM4 Max隔離認証（commit `6e1d1f8`）では、`original_metal_visual` は960×540、warm-up 10 + 計測120フレームで、通常画像・uniform-noiseの双方とも37/37件が平均/p95 30fps gateを通過しました。CPU数値参照とは34/37件が規定範囲内、残り3件もエッジ方向・エッジ量・粗い構造による原作スタイル形態gateを通過しています。全144名を処理する `compat_realtime` は引き続き明示的に別の視覚近似です。

リアルタイム経路には14機構があります。従来のブロック破損に加え、水平／垂直ティア、RGBチャンネルシア、アナログ同期崩れ、ミラーフォールド、輪郭エコー、ビットプレーン・ディザ、波形ワープ、ポスタライズ／ソラリゼーション、タイル入れ替え、斜めスリップ、走査線ウィーブ、四象限ミラーを独立した空間構造として実装しています。いずれも1 passのCPU/Metal実装で、フレームごとの確保を行いません。`--strength` は `0`（無加工）から `2`（最大）で、`--effect-amount`、`--effect-scale`、`--effect-rate` で機構固有の形状を制御します。

macOSでMetal shaderをビルドする際はFull Xcodeが必要です。CMakeはデフォルトで `/Applications/Xcode.app/Contents/Developer` を使用するため、システムの`xcode-select`設定を変更する必要はありません。

### Webカメラ・リアルタイムプレビュー

macOS版は`GLIC Webcam Preview.app`を生成します。内蔵または外付けカメラを
960×540・30fpsで取得し、画面上部または`Lane`メニューから`Original Visual`、
`Spatial Metal`、`Codec Glitch`を切り替えます。採用済み19 presetのみを表示し、
内訳はOriginal 14、Spatial 4、Codec 1です。Original Visualでは原作準拠の
`Strict`、fp32 CDF97と2フレーム分割木再利用を使う`Fast Match`、画像解析allowlist
から安全なpresetだけFastへ切り替える`Auto 20fps`を選べます。Codec Glitchでは
採用されたH.264 presetの`Amount`と、codec historyを消去する`Reset`を操作できます。
処理時間、GPU/codec latency、処理fps、実効モード、drop数を画面上で確認できます。

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

### Codec Glitch（H.264 / HEVC / ProRes + offline 8 codec）

`codec_glitch`は、VideoToolbox hardware encoder/decoderとMetal互換
`CVPixelBuffer`/post pathを使うmacOS専用の非同期動画レーンです。`.glic`ファイル
codec、37 presetの`original_visual`、全144 presetを視覚近似する
`compat_realtime`とは別で、GLIC presetの意味や原作とのpixel一致を主張しません。

```bash
python3 scripts/process_video.py input.mov output-codec.mp4 \
  --processing-mode codec_glitch \
  --codec-format hevc \
  --codec-effect slice_transplant \
  --codec-amount 0.52 --codec-rate 0.34 --codec-feedback 0.58 \
  --seed 0x474c4943 \
  --width 960 --height 540 --fps 30 \
  --report output-codec.json --overwrite
```

36 effectは`qp_pump`、`bitrate_crush`、`slice_dropout`、
`slice_transplant`、`pframe_loss`、`idr_starvation`、`payload_xor`、
`reference_timewarp`、`codec_feedback`、`generation_cascade`、
`resolution_hop`、`chroma_codec_echo`、`temporal_polyphony`、
`intra_cannibalism`、`residual_rift`、`codec_grain_synth`、
`recursive_codec_skin`、`concealment_choreography`と、Realtime Crossbreedの
`dual_codec_crossbreed`、`codec_pingpong`、`gop_accordion`、`bframe_braid`、
`plane_split_codec`、`roi_quality_islands`、`codec_phase_mosaic`、
`encoder_hot_swap`、`pts_rubberband`、`bitrate_raster`と、追加8種の
`plane_time_split`、`reference_atlas`、`flow_lattice`、`scan_order_fold`、
`regional_gop_clock`、`entropy_feedback`、`rolling_time_shutter`、
`asymmetric_plane_codec`です。全effectが圧縮H.264の
VCL byteを変更せず、
VideoToolboxでclean decodeします。`slice_dropout`と`slice_transplant`はdecode履歴の
水平row／帯を合成し、`payload_xor`はposterize、RGB組み替え、位置をずらした
macroblock状tileでdigital damageを作ります。`reference_timewarp`は4〜12 frameへ
設定できるdecode済み`CVPixelBuffer`履歴から過去frameを選び、圧縮P packetを再利用しません。
`resolution_hop`は1/2または1/4 codec段の復元時にpixel化を加えます。
新しい6 effectは、複数時点の領域合成、再帰的な自己block copy、予測と残差の
再合成、GPU grain、復元filter feedback、領域別concealmentをMetal-backed pathで
実装します。
`pframe_loss`と`idr_starvation`だけはencode済みframeを意図的にholdし、直前の正常な
decode結果をrepeatします。

`prepare`は通常stageのhardware encoderでbackendを検証し、QP/cascade/縮小encoderと
decoderは最初の利用時に遅延生成します。VideoToolboxの`RealTime`とlow-latency rate
controlは既定で有効です。動的bitrateの安全floorは既定4,000,000 bpsの
`averageBitRate`を超えない`min(averageBitRate, width * height * fps / 4)`で、強い
rate変更によるhardware encoderのframe dropを防ぎます。`bitrate_crush`とcascadeはfloor
到達後もMetal-backed圧縮模様を加えて視覚差を保ちます。新規stageだけencode/decode
500/300ms、定常時は100/45msのdeadlineを使います。

設計・計測目標は960×540・30fps（33.33ms）、実運用のhard floorは20fps・p95
50msです。これは全Macでの保証ではありません。動画JSONは意図的holdを
`intentional_repeat_frames`、障害fallbackを`fallback_frames`へ分離します。
最初のdecode前に失敗したframeはretain済みfull-size入力を出し、
`non_intentional_fallback_frame=true`にします。
`reliability_passed`は非意図的fallback、全codec処理error、watchdog recovery、
backpressure drop、output queue dropがすべて0の場合だけtrueです。legacy fieldの
`poll_queue_drops`はcallback/poll両方のdropを合算します。20fps合格にはさらに
960×540以上、120 frame以上、frame数維持、hardware codec、実測/stream 20fps以上、
p95 50ms以下が必要です。
複数effectの動画比較と非類似rankingには
`scripts/evaluate_codec_glitch_videos.py`を使います。
詳細とC APIは[Codec Glitch](docs/CODEC_GLITCH.md)と
[Multi-codec guide](docs/MULTICODEC_GLITCH.md)、
[Glitch expansion catalog](docs/GLITCH_EXPANSION.md)、
[Embedding guide](docs/EMBEDDING.md#codec-glitch-c-api-macos-only)を参照してください。

AV1 / VP9 / Theora / DiracはFFmpeg、AV2は公式AVM v1.0.0、VVCは公式
Fraunhofer VVenC v1.14.0で、実際のencode/decode世代を作ります。これらを
VideoToolbox realtimeとは主張しません。
各世代のbitstreamとSHA-256を残す共通runnerは次の通りです。

```bash
python3 scripts/build_av2_reference.py
python3 scripts/process_multicodec_glitch.py input.mov output-av2.mp4 \
  --codec av2 --effect generation_cascade --generations 2 \
  --width 480 --height 270 --fps 15
```

圧縮packet、NAL/OBU、timestampを直接変化させる処理は、リアルタイム経路から
分離したOffline Packet Labで行います。8 effectはH.264 / HEVC / AV1 / VP9 /
ProResの対応範囲をfail-closedで検証し、破損decodeをtimeout・CPU・出力容量制限付き
subprocessへ隔離します。

```bash
python3 scripts/process_offline_packet_glitch.py input.mov packet-glitch.mp4 \
  --codec h264 --effect packet_bit_rot --amount 0.68
```

frame欠落後のpreviewは長さが異なるため、
`scripts/evaluate_offline_packet_glitches.py`が正規化時間位置の視覚差、時間差、
decode生存率をまとめてrankingします。詳細は
[Offline Packet Glitch Lab](docs/OFFLINE_PACKET_GLITCH.md)を参照してください。

motion/residual/reference、semantic/depth/audio、実multi-decoder合成、異種codec直列処理、
token-free自動探索は[Codec Lab](docs/CODEC_LAB.md)へ分離しています。
さらにMPEG-2の圧縮motion vector、量子化DCT係数、quantizer scaleを直接変更する
12 effectと、MPEG-4 Part 2の圧縮MVを直接変更する4 variantは、独立した
FFglitch transplication経路です。

```bash
FFEDIT="$(python3 scripts/install_ffglitch_reference.py --print-ffedit)"
python3 scripts/process_native_syntax_glitch.py input.mov direct.mp4 \
  --effect compressed_motion_vector_vortex --ffedit "$FFEDIT"
```

H.264/HEVCのentropy field直接編集は未実装でfail-closedします。詳細と証跡契約は
[Native Compressed Syntax Glitch](docs/NATIVE_SYNTAX_GLITCH.md)を参照してください。
全16 variantの実動画差分と非類似性rankingは次で自動生成できます。

```bash
python3 scripts/evaluate_native_syntax_glitches.py input.mov \
  --output-dir search-runs/native-syntax --codec all --resume
```

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

`glic_realtime_search` は外部APIやLLMを呼ばず、決定的なMAP-Elites探索で技術的に異なる候補を収集します。上流GLICの144 presetを全て読み込み、色空間・3チャンネルのblock範囲、segmentation、prediction、quantization、transform、wavelet、compression、scale、encodingの実値をseedにします。各14候補のsweepは、原作値そのもの、原作値の弱／強変異、同一機構archiveの子、完全randomの5 laneを順番に試します。RGB直加工の機構でも原作codec値のhashからamount、scale、rate、strengthを導出するため、名前だけでなく実値が形状へ影響します。原作との対応は候補ごとに`source_preset`と`source_preset_mapping`（`exact-compatible` / `approximated` / `unsupported`）へ記録します。

14機構を常に同数試すため、一種類のブロック解像度へ収束しません。複数入力・2 seed・8 frame phaseでMetal出力を評価し、無変化、過剰破壊、クリッピング、入力非依存ノイズを除外します。archiveへ入る可能性がある候補だけを、別の永続Metal backendで960×540・10 frame warm-up・120 frame計測し、wall-clockの平均とp95が両方33.333ms以下の場合に限ってeliteとして保存します。recipe v1は従来互換の `legacy_block` として読み込めます。

探索archiveは、技術ゲート、独立評価family、量子化Pareto front、無加工との差分形状cluster、多様性制約の順に絞り込みます。画像解析は16:9を維持し、2/4/8/16/32/64/128pxの支配的な変化スケール、方向、差分領域、空間分布を測ります。Top 8は8種類の機構、4種類以上のスケール、1スケール最大2件、mega-scale最大1件、形状距離の下限を必須にします。条件を満たす組がなければ似た候補で穴埋めせず、`publishable=false` にします。ランキングは同じarchive snapshotを960×540で再認証し、Metal、warm-up 10以上、計測120 frame以上、平均・p95とも30fps以上という証明が欠ける候補をfail-closedで除外します。

画像解析は無加工フレームとの残差を主軸にし、色相差による見かけ上の多様性より、破損のスケール・方向・位置を重く評価します。代表PNGは静止画なので、`temporal_residual_delta` は時間変化量であって光学flowではありません。美的な「最適」を断定するものではなく、目視する候補を説明可能で似ていない少数へ絞る仕組みです。Full HDを線形1/4へ縮小済みの480×270入力では、追加縮小を避けるため `--scale 1` を指定します。

```bash
./build/glic_realtime_search \
  --input test-videos/search-inputs/video_00.png \
  --input test-videos/search-inputs/video_01.png \
  --presets-dir presets \
  --output-dir search-runs/pilot \
  --duration-seconds 18000 --backend metal --scale 1

scripts/build_ranked_catalog.sh search-runs/pilot
```

`ranking.html`、全監査用の `ranking.json` / `ranking.csv`、Top 32の `shortlist.json`、Top 64の `selection.json` が生成されます。`performance-certifications.json` はarchive SHA、レシピSHA、認証binary・Metal shader library・入力・ハードウェアidentityと、各候補のmean/p95/p99/maxを保持します。未認証、CPU、誤解像度、120 frame未満、平均またはp95が33.333msを超える候補でTop枠を補完しません。`generation-directives.json` には過密な知覚cluster、類似出力が多いrecipe family、unique clusterが少ないarchive cellを保存します。画像解析と性能認証はcacheされ、archive世代不一致時には部分結果を公開せず直前のatomic reportを保持します。

適度な複雑さに限定し、以前のランキング画像とも今回の選択内でも似ていない
canonical presetを抽出するには、二段目の選別器を実行します。

```bash
python3 scripts/select_novel_moderate_presets.py search-runs/pilot \
  --reference-ranking search-runs/previous/ranking.json --count 48
```

出力には再利用可能な`presets.json`、CSV、HTML、contact sheetと、分布確認用の
`embedding-features.csv`が含まれます。HTMLでは各動画を見ながら「採用する」を
チェックできます。選択はブラウザに保存され、「採用JSONを保存」または
「CSVを保存」から、チェックしたpresetだけを`adopted-presets.json` / CSVへ
書き出せます。初回表示では全候補が未選択です。複雑さの上下20%と、過去画像への距離の
下位20%を除外してから、機構・artifact scale・方向・上流由来かsyntheticかを組み合わせた
層を横断し、候補間距離にも下限を設けるmax-min選択を行います。同じ上流presetからの
派生数も動的に制限するため、似た変異で枠を埋めません。

生成したレビュー画面は、MP4のシークに対応するbyte-range serverで配信できます。

```bash
cd search-runs/pilot/novel-moderate-selection
python3 ../../../server.py 8888
```

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
- Eighteen VideoToolbox H.264 / HEVC / ProRes codec effects with a Metal-backed post path
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

### How the Java/Processing version was accelerated

The original GLIC is a Java/Processing work for interactively encoding and
decoding still images. The `frameRate(20)` call in
[`GLIC.pde`](https://github.com/GlitchCodec/GLIC/blob/460e61bf9b01f7415cf973b3d655a0ae2c7962a7/GLIC.pde#L49-L124)
controls display refresh; it does not show that the codec processes 20 frames
per second. This project keeps the file codec but adds a separately designed
live-video path. The acceleration is therefore more than a Java-to-C++ rewrite:
it changes where work runs, what can run in parallel, how long memory lives,
and how frames move through the application.

| Area | Original Java/Processing version | GLIC Metal realtime path |
|---|---|---|
| Data path | Synchronous still-image encode/decode operations | Applies the effect directly to in-memory frames, bypassing `.glic` serialization, entropy coding, file I/O, and immediate re-decoding |
| Compute | Codec work runs on the CPU | Moves prediction, quantization, CDF 9/7 FWT/WPT, inverse reconstruction, and spatial glitches to Metal compute |
| Parallelism | Preserves the original recursive quadtree and random-consumption order | Runs independent channels, color-conversion slices, and leaves within each dependency frontier concurrently |
| Memory | Temporary arrays and transform matrices suit still-image processing | Allocates planes, leaf descriptors, transform scratch, Metal buffers, and CPU workers in `prepare()`; normal frame processing does not reallocate them |
| Segmentation | Evaluates adaptive segmentation for each input | Caches fixed-block trees, stops adaptive image reads once the variance decision is final, and can reuse adaptive trees in Fast Match |
| GPU submission | Not applicable | Encodes the original-style frame into one command buffer with one completion wait and no mapped-buffer copy on Apple unified memory |
| Live delivery | Synchronous Processing UI | Separates capture and processing, selects the newest of three preallocated slots, and drops stale waiting frames instead of accumulating latency |
| Video codec glitch | Not applicable | Runs VideoToolbox H.264 / HEVC / ProRes encode/decode plus a Metal post path through asynchronous bounded queues |

The `Strict` original-style lane deliberately keeps quadtree control on the
CPU. Changing the Processing-compatible 48-bit RNG consumption, channel order,
or DFS order would produce a different image. Once the exact leaves are known,
independent reconstruction moves to the GPU: small leaves use threadgroup
memory, while large adaptive CDF97 leaves use a preallocated global workspace.
Fixed geometry is built once, and skipped random calls use RNG skip-ahead so
the terminal state remains identical. This separates fidelity-preserving
optimization from Fast Match, which may reuse a visually equivalent tree for
more live-processing headroom.

| Processing mode | Performance and compatibility position |
|---|---|
| `original_visual` / Strict | Prioritizes the original style for the 37 supported presets, with exact segmentation order and Metal reconstruction |
| `original_visual` / Fast Match | Allows fp32 CDF97 and adaptive-tree reuse for additional realtime headroom |
| `compat_realtime` | The lightest path, mapping all 144 presets to one-pass Metal spatial approximations |
| `codec_glitch` | Uses temporal state in the hardware video codec and makes no upstream-preset compatibility claim |

Performance and fidelity are tested separately. Benchmarks gate p95 as well as
the post-warm-up mean, drops, and backpressure. Image validation checks a CPU
reference, leaf-order hashes, terminal RNG state, SSIM, color difference, and
edge difference. Because the Metal CDF97 intermediate matrix remains fp32, the
project does not claim pixel identity with CPU/JWave double. The supported
boundary, measurements, and known differences are recorded in
[Original-preset realtime compatibility](docs/ORIGINAL_PRESET_REALTIME.md).

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

The public realtime API is declared in [include/glic_metal/glic_metal.h](include/glic_metal/glic_metal.h). Coding agents should follow the [AI integration contract](docs/AI_INTEGRATION.md). The CPU backend reuses resolution-sized workspaces after preparation. The Metal backend provides a synchronous CPU-buffer API, an opaque zero-copy `MTLTexture` API, and a non-blocking API that appends work to the caller's `MTLCommandBuffer`.

See [docs/ORIGINAL_PRESET_REALTIME.md](docs/ORIGINAL_PRESET_REALTIME.md) for compatibility levels and why upstream's 20 fps setting is a UI rate rather than codec throughput. In the previous isolated M4 Max certification (`6e1d1f8`, 2026-07-20), `original_metal_visual` passed the mean+p95 30 fps gate for all 37 supported presets on both the normal and uniform-noise inputs. Numeric CPU-reference comparison passed 34/37; the remaining three passed the separate blurred-structure and edge-morphology gate. The all-144 Metal path remains the separately labelled `compat_realtime` visual approximation.

The realtime path has fourteen explicit glitch mechanisms. `legacy_block` preserves the preset-derived codec damage path; the other thirteen are independent RGB mechanisms so selecting a legacy preset cannot collapse them back into the same block topology.

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
| `tile_shuffle` | Coherent rectangular tile relocation |
| `vertical_tear` | Narrow columns displaced vertically |
| `diagonal_slip` | Opposing diagonal band displacement with chroma slip |
| `scanline_weave` | Alternating row groups pulled in opposite directions |
| `quad_mirror` | Mirrored cells repeated on both axes |

`--strength` ranges from `0` (off) to `2` (maximum). The explicit families also expose normalized `amount`, `scale`, and `rate` controls. Corruption patterns use a reproducible 32-bit seed and are held for several frames according to `rate`.

Full Xcode is required to compile the Metal shader on macOS. CMake uses `/Applications/Xcode.app/Contents/Developer` by default, so it does not need to change the system `xcode-select` setting.

### Realtime webcam preview

The macOS build produces `GLIC Webcam Preview.app`. It captures a built-in or
external camera at 960×540/30 fps and switches between `Original Visual`,
`Spatial Metal`, and `Codec Glitch` from the popup or `Lane` application menu.
It exposes only the adopted 19 presets: 14 Original, four Spatial, and one
Codec. Original Visual offers original-compatible `Strict`, fp32 CDF97
with two-frame tree reuse in `Fast Match`, and fail-closed `Auto 20fps`.
Codec Glitch offers the adopted H.264 preset, an `Amount` control, and `Reset`
for clearing codec history. Processed fps, total/GPU or codec latency,
effective mode, recovery state, and dropped frames remain visible.

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

### Codec Glitch (H.264 / HEVC / ProRes + eight offline codecs)

`codec_glitch` is a macOS-only asynchronous video lane built from the
VideoToolbox hardware encoder/decoder and Metal-compatible `CVPixelBuffer`/post
path. It is distinct from the `.glic` file codec, 37-preset `original_visual`,
and all-144 visual approximation in `compat_realtime`. It does not apply GLIC
preset semantics or claim upstream pixel equivalence.

```bash
python3 scripts/process_video.py input.mov output-codec.mp4 \
  --processing-mode codec_glitch \
  --codec-format hevc \
  --codec-effect slice_transplant \
  --codec-amount 0.52 --codec-rate 0.34 --codec-feedback 0.58 \
  --seed 0x474c4943 \
  --width 960 --height 540 --fps 30 \
  --report output-codec.json --overwrite
```

The 36 effects include `qp_pump`, `bitrate_crush`, `slice_dropout`,
`slice_transplant`, `pframe_loss`, `idr_starvation`, `payload_xor`,
`reference_timewarp`, `codec_feedback`, `generation_cascade`,
`resolution_hop`, `chroma_codec_echo`, `temporal_polyphony`,
`intra_cannibalism`, `residual_rift`, `codec_grain_synth`,
`recursive_codec_skin`, `concealment_choreography`, ten Realtime Crossbreed
effects from `dual_codec_crossbreed` through `bitrate_raster`, and the eight
decoded-history/Metal effects from `plane_time_split` through
`asymmetric_plane_codec`.
Every effect sends
unchanged H.264 VCL bytes through a clean VideoToolbox decode. `slice_dropout` and
`slice_transplant` composite horizontal rows/bands from decoded history;
`payload_xor` creates digital damage with posterization, RGB rewiring, and
displaced macroblock-like tiles. `reference_timewarp` selects an older frame
from decoded `CVPixelBuffer` history configured from four to twelve frames
instead of reusing a compressed P packet. `resolution_hop` adds pixelation
while restoring its one-half or one-quarter-resolution codec result.
The six additional effects use Metal-backed multi-age regional composition,
recursive self-copy, prediction/residual recomposition, synthesized grain,
restoration feedback, and regional concealment. Only `pframe_loss` and
`idr_starvation` intentionally hold encoded frames and repeat the prior good
decoded result.

`prepare` validates the backend with the normal-stage hardware encoder;
specialized QP/cascade/downscale encoders and the decoder are created on first
use. VideoToolbox `RealTime` and low-latency rate control are enabled by
default. The default average bitrate is 4,000,000 bps, and the dynamic floor is
`min(averageBitRate, width * height * fps / 4)`, so it never raises the host
setting. `bitrate_crush` and cascade add Metal-backed compression patterns after
reaching that floor. New stages use 500/300 ms encode/decode deadlines; steady
state returns to 100/45 ms.

The design and measurement target is 960×540 at 30 fps (33.33 ms); the live
hard floor is 20 fps with p95 at or below 50 ms. This is not a guarantee for
every Mac. The video JSON separates intended holds in
`intentional_repeat_frames` from failure `fallback_frames`.
If a failure occurs before the first decode, the retained full-size input is
emitted with `non_intentional_fallback_frame=true`.
`reliability_passed` requires zero non-intentional fallback, codec-processing
errors, watchdog recoveries, backpressure drops, and output-queue drops. The
legacy `poll_queue_drops` field combines callback and polling delivery losses.
The 20 fps pass also requires at least 960×540, at least 120 frames, preserved
frame count, hardware encode/decode, processing and stream rates of at least
20 fps, and p95 at or below 50 ms. See
[Codec Glitch](docs/CODEC_GLITCH.md) and the
[multi-codec guide](docs/MULTICODEC_GLITCH.md), plus the
[glitch expansion catalog](docs/GLITCH_EXPANSION.md) and
[embedding guide](docs/EMBEDDING.md#codec-glitch-c-api-macos-only).
Use `scripts/evaluate_codec_glitch_videos.py` for dry/wet analysis and
diversity ranking across rendered effects.

AV1, VP9, Theora, and Dirac use explicit FFmpeg encoders/decoders. AV2 uses
the pinned official AVM v1.0.0 tools and VVC uses pinned official Fraunhofer
VVenC v1.14.0. Missing reference tools fail closed. The common offline runner
retains every compressed generation and makes no realtime claim:

```bash
python3 scripts/process_multicodec_glitch.py input.mov output-av1.mp4 \
  --codec av1 --effect temporal_echo --generations 2
```

Compressed packet, NAL/OBU, and timestamp mutation lives in a separate Offline
Packet Lab. Its eight effects cover fail-closed subsets of H.264, HEVC, AV1, VP9,
and ProRes. Damaged decode runs in a subprocess with timeout, CPU, output-size,
and descriptor limits; it is never presented as realtime.

```bash
python3 scripts/process_offline_packet_glitch.py input.mov packet-glitch.mp4 \
  --codec vp9 --effect vp9_superframe_shuffle --amount 0.68
```

Use `scripts/evaluate_offline_packet_glitches.py` for normalized-timeline
visual/temporal analysis and decode-survival ranking when outputs have unequal
frame counts. See
[Offline Packet Glitch Lab](docs/OFFLINE_PACKET_GLITCH.md).

Motion/residual/reference reconstruction, semantic/depth/audio processing,
true multi-decoder blending, cross-codec chains, and token-free evolutionary
search are documented in [Codec Lab](docs/CODEC_LAB.md).

Twelve effects directly mutate MPEG-2 encoded motion vectors, quantized DCT
coefficients, or quantizer scales through FFglitch transplication. Four
additional codec-effect variants apply the MV family to MPEG-4 Part 2. This is
a separate offline process with retained syntax JSON and before/after
bitstreams; it is not a decoded reconstruction proxy. A token-free batch tool
ranks actual-video difference and diversity across all 16 variants.
H.264/HEVC entropy-field editing is not implemented and fails closed. See
[Native Compressed Syntax Glitch](docs/NATIVE_SYNTAX_GLITCH.md).

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

`glic_realtime_search` is a deterministic, API-free MAP-Elites search for technically diverse preset candidates. It loads all 144 upstream GLIC presets and seeds candidates from their actual colorspace, three-channel block ranges, segmentation, prediction, quantization, transform, wavelet, compression, scale, and encoding values. Each fourteen-family sweep rotates through five lanes: upstream base, light upstream mutation, same-family archive mutation, strong upstream mutation, and fully random generation. For direct RGB families, a hash of the upstream codec values drives active amount, scale, rate, and strength controls, so the source values affect the rendered shape rather than only metadata. Every derived row records `source_preset` and `source_preset_mapping` (`exact-compatible`, `approximated`, or `unsupported`).

Recipe v2 cycles all fourteen explicit mechanisms evenly. After the low-resolution visual gates, only candidates that could enter the mechanism-prefixed archive are certified on a separate persistent Metal backend at 960x540 with 10 warm-up and 120 measured frames. A candidate becomes an elite only when both mean and p95 synchronous wall time are at most 33.333 ms. Recipe v1 remains readable as `legacy_block`.

The archive is narrowed in stages: technical gates, score families, quantized Pareto fronts, dry/wet residual morphology clusters, and diversity selection. Analysis preserves 16:9 geometry and measures 2/4/8/16/32/64/128px artifact scales, orientation, residual coverage, and spatial grids. The Top 8 must cover eight mechanisms, at least four scale buckets, no more than two candidates per bucket, at most one mega-scale result, and a minimum morphology distance. If a feasible set does not exist the report is marked `publishable=false` instead of filling it with lookalikes. The ranking pipeline independently certifies the exact archive snapshot and fails closed unless every row has a matching recipe identity and Metal 960x540 measurement. Missing certification, CPU results, fewer than 120 frames, or mean/p95 above the 30 fps budget are excluded and never used to fill Top 12/32/64.

An external, self-tested `visual-liveliness` instrument measures presence and connected-component shape. The repository analyzer adds pHash/dHash, HSV, color, edge, and block-boundary descriptors. A representative PNG is still only one frame, so `temporal_residual_delta` is labelled activity rather than optical flow. This is deterministic technical/perceptual triage, not a claim of learned aesthetic optimality. Inputs already reduced from Full HD to 480×270 should use `--scale 1`.

```bash
./build/glic_realtime_search \
  --input test-videos/search-inputs/video_00.png \
  --input test-videos/search-inputs/video_01.png \
  --presets-dir presets \
  --output-dir search-runs/pilot \
  --duration-seconds 18000 --backend metal --scale 1

scripts/build_ranked_catalog.sh search-runs/pilot
```

The pipeline writes `ranking.html`, full-audit `ranking.json` / `ranking.csv`, Top-32 `shortlist.json`, Top-64 `selection.json`, and `performance-certifications.json`. The certification sidecar records archive/recipe identities, certifier binary, Metal shader library, input and hardware identities, plus mean/p95/p99/max timings for every elite. Performance and image-analysis caches only reuse matching identities. Missing rows, failed self-tests, or mismatched archive generations fail closed and preserve the previous atomic reports.

Run the second-stage selector to keep middle-complexity canonical presets that
remain visually distant from both a prior ranking and the current selection:

```bash
python3 scripts/select_novel_moderate_presets.py search-runs/pilot \
  --reference-ranking search-runs/previous/ranking.json --count 48
```

It writes a runnable `presets.json` bank, CSV, HTML, contact sheet, and
`embedding-features.csv`. In the HTML review page, each video has an
**Adopt** checkbox. The browser remembers the review state, and the JSON/CSV
buttons export only checked presets as `adopted-presets.json` or CSV. Every
candidate is unchecked on the first visit. The outer 20% complexity tails and the lowest 20%
prior-novelty tail are removed before max-min selection across mechanism,
artifact scale, orientation, and upstream/synthetic origin. The selector also
enforces a candidate-distance floor and dynamically limits repeated variants
from the same upstream preset.

Serve a generated review directory with byte-range support so all MP4 cards
can seek reliably:

```bash
cd search-runs/pilot/novel-moderate-selection
python3 ../../../server.py 8888
```

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
