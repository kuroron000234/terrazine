# TERRAZINE

日本・英語圏・中華圏のテックニュース（AI / 開発 / ハード）を集めて、1ページで読むための個人用サイト。
大手メディアより、はてブ・Zenn・5ch・Reddit・HN・V2EX など「早くて実用寄り」のソースを優先している。

## 仕組み

- `feeds.toml` … 収集するフィードの一覧。追加・削除はここだけ（5ch は `type = "5ch"` で板URLを指定）
- `build.py` … フィードを取得 → キーワードでタグ付け → サムネイル取得（フィード内画像、なければ記事の og:image）→ `site/index.html` と `site/items.json` を生成
- `web/index.html` … ページのテンプレート（データはビルド時に埋め込み）
- `.github/workflows/build.yml` … 毎時実行して GitHub Pages に公開。前回の `items.json` とマージするので、記事は7日間残る

## ローカルで動かす

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py
# site/index.html をブラウザで開く
```

## 公開（GitHub Pages）

1. GitHub にリポジトリを作って push
2. Settings → Pages → Source を **GitHub Actions** にする
3. Actions タブで `build` を手動実行（以降は毎時自動）

## メモ

- `filter = true` のフィードは `build.py` の `KEYWORDS` に一致した記事だけ残す
- 記事に `title_ja` / `summary_ja` があればページに表示される（翻訳を後で足すときの差し込み口。マージ時も保持される）
- 翻訳が入るまでは各記事の「日本語で読む」から Google 翻訳経由で読める
- Reddit は未認証だとレート制限が厳しいので、サブレディットを `r/A+B+C` でまとめて1回だけ取得している
