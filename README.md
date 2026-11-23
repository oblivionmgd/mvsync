# MVSync

DJ ミックスの音を聞いて、いま流れている曲の **ミュージックビデオ（MV）を自動で選び、曲の位置と速度に合わせて TouchDesigner で再生する** ツールです。

*MVSync listens to a DJ mix, recognises the song, finds its official music video and plays it in TouchDesigner in sync with the audio (position and tempo).*

- 自分で用意した MV ライブラリ（YouTube の URL / プレイリスト）と音で照合し、位置と再生速度（±16 %）を合わせる
- ライブラリにない曲は Shazam（ShazamIO）で曲名を調べ、YouTube の公式 MV を探して音で確かめてから流す
- Remix の曲は Remix の MV、なければ原曲の MV を頭から流す
- TouchDesigner 側は `mvsync.tox` を置くだけ。出力は TOP（`out1`）と状態の CHOP（`out_ctrl`）

> 動作確認環境: macOS（Apple Silicon / Intel）、TouchDesigner 2025.33230、Python 3.12。Windows 10 / 11 は確認中です。

## 仕組み

```
DJ の音 ──(BlackHole など)──▶ worker（Python, TD の外）
                                 │ 音の照合 / Shazam / YouTube 検索・取り込み (yt-dlp)
                                 ▼ OSC (127.0.0.1:9100)
                         mvsync.tox（TouchDesigner）── MV を位置・速度を合わせて再生 ──▶ out1
```

重い処理はすべて外部の worker プロセスで行い、TD のメインスレッドは OSC の受信と再生クロックだけを担当します。worker は tox から自動で起動・停止します。

## 必要なもの

- TouchDesigner 2025 以降
- DJ の音を PC に入れる手段（例: macOS は [BlackHole](https://github.com/ExistentialAudio/BlackHole)、Windows は [VB-CABLE](https://vb-audio.com/Cable/)、またはオーディオインターフェースの入力）
- インターネット接続

Python・yt-dlp・ffmpeg・Deno などは、TD の **Install** ボタンが自動で入れます。別にインストールするものはありません。

## インストール

1. [Releases](../../releases) から `mvsync.tox` をダウンロードし、TouchDesigner のネットワークにドラッグします。
2. COMP の **Worker** ページで **Install / Repair** を押します。
   - 初回は 1〜数分かかります（Python 3.12 と必要なパッケージ、約 400 MB をダウンロード）。
   - **Install status** が「準備完了」になれば終わりです。進み具合もここに出ます。
   - 入る場所は `<Library folder>/app`（既定 `~/MVSync/app`）だけです。消すときはこのフォルダを消してください。
3. **Config** ページの **Audio device** を確認します。空欄なら OS の既定の入力デバイスを使います（Windows は「サウンド」設定の録音の既定のデバイス、macOS は「サウンド」設定の入力）。別のデバイスを使うときは、その名前の一部（例: `BlackHole`、`CABLE Output`）を入れてください。見つからないときは、使える入力デバイスの一覧が Status ページの **Last log** に出ます。
4. **Worker** ページの **Start** を押します。Status ページの Worker が「動作中」になれば準備完了です。

新しいバージョンの tox に入れ替えたときは、Install status に「更新があります」と出るので、もう一度 **Install / Repair** を押してください。

### Config ページの場所の設定（通常は空欄のまま）

- **Library folder** … MV・設定・ログ・worker の保存先（既定 `~/MVSync`）
- **Code folder** … 空欄なら `<Library folder>/app`（Install で入る場所）を使います。リポジトリのソースで動かすときだけ指定します。
- **Worker Python / yt-dlp path / node folder** … 空欄なら自動で探します。

### ターミナルで入れる場合

[Releases](../../releases) の `mvsync-<version>.zip` を展開し、`./setup.sh`（Windows は `setup.bat`）を実行すると、同じものがそのフォルダの `.venv` に入ります。この場合は Config の **Code folder** に展開したフォルダを指定してください。

## 使い方

### MV ライブラリを作る

`<Library folder>/urls.txt` に YouTube の URL を 1 行に 1 つ書きます（初回起動で空のファイルができます）。動画 URL とプレイリスト URL のどちらも使えます。`#` 以降はコメントです。

```
https://www.youtube.com/watch?v=XXXXXXXXXXX
https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx
```

保存すると worker が自動でダウンロードして照合用のデータを作ります（Library ページで進み具合を確認できます）。プレイリストごとに **Active playlist** で照合の対象を絞れます。

ライブラリが空でも、**Online fallback** が On なら Shazam と YouTube 検索だけで動きます（MV が出るまで少し時間がかかります）。

### 出力

- `out1`（TOP）… 同期した MV。曲がないときは **When silent** で黒か最後のフレーム
- `out_ctrl`（CHOP）… `state` `conf` `rate` `bpm_est` `locked` `track_changed`

`state` の値:

| 値 | 状態 | 意味 |
|---|---|---|
| 0 | IDLE | 無音 |
| 1 | SEARCHING | 照合中 |
| 2 | LOCKED | MV と位置が確定 |
| 3 | HOLD | 一時的に見失った（再生は続ける） |
| 4 | LOST | 見失った |
| 5 | IDENTIFYING_ONLINE | Shazam で曲名を調べている |
| 6 | FETCHING | MV を取得中 |
| 7 | NO_MV | 公式の MV が見つからない曲 |
| 8 | PROVISIONAL | オンラインで見つけた MV を仮の位置で再生中 |
| 9 | REMIX | Remix の曲で原曲の MV を頭から再生中 |

### 主な操作（Control ページ）

- **Offset / Nudge** … 映像のタイミングを ms 単位で調整（下の「音と映像のタイミングを合わせる」）
- **Relock** … 照合をやり直す
- **Reject current MV** … 今の MV を誤りとして外す
- **Manual select** … MV を手動で選ぶ（auto で自動に戻る）

### 音と映像のタイミングを合わせる（Offset）

worker は「音を受け取った時刻」を基準に MV の位置を決めます。仮想オーディオデバイス（BlackHole / VB-CABLE など）やオーディオインターフェースを通すと、スピーカーから聞こえる音より少し遅れて worker に届くため、その分だけ映像が遅れて見えます。遅れはデバイスや設定（バッファサイズなど）ごとに違うので、環境ごとに一度合わせてください。

1. 口の動きや打楽器など、タイミングの分かりやすい MV が LOCKED の状態で流れているときに見比べます。
2. 映像が音より **遅れて** 見えるなら **Offset をプラス**に、**早く** 見えるならマイナスにします（単位は ms）。Offset を 10〜50 ms ずつ動かして合わせます。
3. 合ったら TD のプロジェクトを保存します（Offset は COMP のパラメータとして保存されます）。

**Nudge -10ms / +10ms** はその曲だけの一時的な調整で、曲が変わると 0 に戻ります。環境全体のずれは Offset で合わせてください。

目安として、VB-CABLE やバッファの大きい設定では数十〜100 ms 以上ずれることがあります。デバイスやバッファサイズを変えたら合わせ直してください。

### 検索の調整（Config ページ）

- **Official MVs only** … オンラインでは公式チャンネル（本人・レーベル）の動画だけを使う（既定 On）
- **Priority words / NG words** … YouTube の動画タイトルで優先する言葉 / 除外する言葉（カンマ区切り）
- **Video quality** … ダウンロードする画質（360 / 720 / 1080）
- **theta_lock / theta_hold / theta_margin** … 照合の確定・維持のしきい値

Shazam への問い合わせは日本語・日本（`shazam_language` = `ja-JP`、`shazam_country` = `JP`）が既定です。変えるときは `<Library folder>/config.json` を編集してください。

## worker を単体で動かす

TD なしで動作を確かめるとき:

```bash
cd ~/MVSync/app
.venv/bin/python worker/mvsync_worker.py --config ~/MVSync/config.json
.venv/bin/python worker/mvsync_worker.py --config ~/MVSync/config.json --input-file song.wav
```

Windows では `%USERPROFILE%\MVSync\app` で `.venv\Scripts\python.exe worker\mvsync_worker.py --config %USERPROFILE%\MVSync\config.json`。

ログは `<Library folder>/logs/` に出ます。

## 開発者向け

- `td/` … tox の中の Text DAT と同じコード。`td/build_mvsync.py` で COMP を一から組み立てられます（TD の Textport で `CODEDIR='<repo>'; exec(open('<repo>/td/build_mvsync.py').read())`。この場合 DAT はファイルと同期します）。
- `tools/build_tox.py` … 配布用の `dist/mvsync.tox` を書き出します。TD 側のコードと、Install で書き出す worker / setup 一式（`mvsync/payload`）を埋め込みます（TD の Textport で `ROOT='<repo>'; VERSION='0.1.0'; exec(open(ROOT + '/tools/build_tox.py').read())`）。
- `tools/make_release.sh <version>` … `dist/mvsync-<version>.zip` を作ります。

## 注意

- YouTube の動画のダウンロードは YouTube の利用規約や各国の著作権法の対象です。権利を持っている・許諾を得ている範囲で、自己責任で使ってください。
- ShazamIO は Shazam の非公式クライアントです。
- Install は uv（https://astral.sh/uv）と、uv が配布する Python 3.12 をダウンロードします。

## ライセンス

MIT License — [LICENSE](LICENSE) を参照してください。
