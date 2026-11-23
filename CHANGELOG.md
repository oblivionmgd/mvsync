# Changelog

## 0.1.0 — 初回公開

- DJ ミックスの音から MV を選び、位置と速度（±16 %）を合わせて TouchDesigner で再生
- ライブラリ（urls.txt の YouTube 動画 / プレイリスト）との音の照合（複数仮説の追跡、LOCK / HOLD / LOST）
- ライブラリにない曲は ShazamIO → YouTube 公式動画の検索 → 音で確認して再生（PROVISIONAL → LOCKED）
- Remix の曲は Remix の MV、なければ原曲の MV を頭から再生（REMIX）
- TD の Config ページで優先ワード / NG ワードを編集
- 配布用: Python・yt-dlp・node・コードフォルダの場所を自動で探す（空欄 = 自動）
