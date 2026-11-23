# MVSync

DJ ミックスの音を聞いて、いま流れている曲の **ミュージックビデオ（MV）を自動で選び、曲の位置と速度に合わせて TouchDesigner で再生する** ツールです。

*MVSync listens to a DJ mix, recognises the song, finds its official music video and plays it in TouchDesigner in sync with the audio (position and tempo).*

- 自分で用意した MV ライブラリ（YouTube の URL / プレイリスト）と音で照合し、位置と再生速度（±16 %）を合わせる
- ライブラリにない曲は Shazam（ShazamIO）で曲名を調べ、YouTube の公式 MV を探して音で確かめてから流す
- Remix の曲は Remix の MV、なければ原曲の MV を頭から流す
- TouchDesigner 側は `mvsync.tox` を置くだけ。出力は TOP（`out1`）と状態の CHOP（`out_ctrl`）

> 動作確認環境: macOS（Apple Silicon / Intel）、TouchDesigner 2025.33230、Python 3.12。Windows は一部対応のコードがありますが未確認です。

## 仕組み

```
DJ の音 ──(BlackHole など)──▶ worker（Python, TD の外）
                                 │ 音の照合 / Shazam / YouTube 検索・取り込み (yt-dlp)
                                 ▼ OSC (127.0.0.1:9100)
                         mvsync.tox（TouchDesigner）── MV を位置・速度を合わせて再生 ──▶ out1
```

重い処理はすべて外部の worker プロセスで行い、TD のメインスレッドは OSC の受信と再生クロックだけを担当します。worker は tox から自動で起動・停止します。

## 必要なもの

| もの | 用途 | 入れ方（macOS） |
|---|---|---|
| TouchDesigner 2025 以降 | 再生 | https://derivative.ca |
| Python 3.12 と [uv](https://docs.astral.sh/uv/)（推奨） | worker | `brew install uv` |
| ffmpeg | 音声の取り出し・720p 以上の結合 | `brew install ffmpeg` |
| Node.js | yt-dlp の YouTube 対応 | `brew install node` |
| 仮想オーディオデバイス（BlackHole など） | DJ の音を worker に渡す | `brew install blackhole-16ch` |

ShazamIO が Python 3.13 以降に対応していないため、worker は Python 3.12 で動かします。

## インストール

1. [Releases](../../releases) から `mvsync-<version>.zip` をダウンロードして、好きな場所に展開します。
2. ターミナルで展開したフォルダに移動し、worker の環境を作ります。

   ```bash
   ./setup.sh
   ```

   フォルダ内に `.venv` ができ、numpy / scipy / sounddevice / python-osc / shazamio / yt-dlp が入ります。
3. TouchDesigner で `mvsync.tox` をネットワークにドラッグします。
4. COMP の **Config** ページを確認します。
   - **Code folder** … 空欄なら tox と同じフォルダ（またはプロジェクトフォルダ）から `worker/` を探します。tox を別の場所に置いた場合は展開したフォルダを指定してください。
   - **Worker Python / yt-dlp path / node folder** … 空欄なら自動で探します（`<Code folder>/.venv/bin/python` など）。
   - **Library folder** … MV の保存先（既定 `~/MVSync`）。
   - **Audio device** … DJ の音が来る入力デバイス名の一部（既定 `BlackHole`）。
5. **Worker** ページの **Start** を押します。Status ページの Worker が「動作中」になれば準備完了です。

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

- **Offset / Nudge** … 映像のタイミングを ms 単位で調整
- **Relock** … 照合をやり直す
- **Reject current MV** … 今の MV を誤りとして外す
- **Manual select** … MV を手動で選ぶ（auto で自動に戻る）

### 検索の調整（Config ページ）

- **Official MVs only** … オンラインでは公式チャンネル（本人・レーベル）の動画だけを使う（既定 On）
- **Priority words / NG words** … YouTube の動画タイトルで優先する言葉 / 除外する言葉（カンマ区切り）
- **Video quality** … ダウンロードする画質（360 / 720 / 1080）
- **theta_lock / theta_hold / theta_margin** … 照合の確定・維持のしきい値

Shazam への問い合わせは日本語・日本（`shazam_language` = `ja-JP`、`shazam_country` = `JP`）が既定です。変えるときは `<Library folder>/config.json` を編集してください。

## worker を単体で動かす

TD なしで動作を確かめるとき:

```bash
.venv/bin/python worker/mvsync_worker.py --config ~/MVSync/config.json
.venv/bin/python worker/mvsync_worker.py --config ~/MVSync/config.json --input-file song.wav
```

ログは `<Library folder>/logs/` に出ます。

## 開発者向け

- `td/` … tox の中の Text DAT と同じコード。`td/build_mvsync.py` で COMP を一から組み立てられます（TD の Textport で `CODEDIR='<repo>'; exec(open('<repo>/td/build_mvsync.py').read())`。この場合 DAT はファイルと同期します）。
- `tools/build_tox.py` … 配布用の `dist/mvsync.tox`（コード埋め込み）を書き出します（TD の Textport で `ROOT='<repo>'; exec(open(ROOT + '/tools/build_tox.py').read())`）。
- `tools/make_release.sh <version>` … `dist/mvsync-<version>.zip` を作ります。

## 注意

- YouTube の動画のダウンロードは YouTube の利用規約や各国の著作権法の対象です。権利を持っている・許諾を得ている範囲で、自己責任で使ってください。
- ShazamIO は Shazam の非公式クライアントです。

## ライセンス

MIT License — [LICENSE](LICENSE) を参照してください。
