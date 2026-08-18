# GEOPLOT SAT — 衛星画像収集ビューア

MGP Pro（Maxar）と Copernicus Data Space Ecosystem を横断し、中国MSA管轄相当の
AOIに含まれる衛星撮影フットプリントを自動収集・地図表示するツール。
サーバー常駐なし、GitHub Actions + 静的HTML（GitHub Pages）で完結する構成。

同種の海事局スクレイパー（geoplot-mil）と同じ設計思想（state.jsonの辞書レベル
マージ、外部cronサービス経由のworkflow_dispatch運用）を踏襲している。

## ファイル構成

```
geoplot-sat.html              地図ビューア本体（単体HTML、ビルド不要）
satellite_scraper.py          スクレイパー本体（MGP Pro / Copernicus検索）
commit_state.py               state.jsonの辞書レベルマージ&コミット補助（GitHub運用専用）
geo_utils.py                  点内判定などの共通ジオメトリ関数
providers/
  mgp.py                       MGP Pro Discovery API クライアント
  copernicus.py                Copernicus OData API クライアント
data/
  china_msa_aoi.geojson        検索対象AOIの境界ポリゴン（v1簡易版）
satellite_out/
  satellite_imagery.geojson    出力（Actionsが生成・コミットする。初回実行前は存在しない）
  state.json                   出力（全件累積state、継続運用に必須）
.github/workflows/scrape.yml   GitHub Actions手動起動ワークフロー（workflow_dispatchのみ）
requirements.txt
```

## セットアップ

### 1. リポジトリを作成してこの一式をpush

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin <あなたのリポジトリURL>
git push -u origin main
```

### 2. GitHub Secretsを登録

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録します。

| Secret名 | 内容 |
|---|---|
| `MGP_API_KEY` | MGP Pro（Maxar）のAPIキー |

Copernicus側は検索に認証が不要なため、Secretの登録は不要です（ダウンロードは
ビューア上のリンクからCopernicus Browser側で都度ログインする方式にしています。
サイズの大きい画像本体をこのリポジトリやActions経由で扱うことはありません）。

### 3. GitHub Pagesを有効化

**Settings → Pages** で、Branch を `main` / `/ (root)` に設定して公開します。
`https://<user>.github.io/<repo>/geoplot-sat.html` でビューアにアクセスできます。

### 4. 定期実行の設定（外部cronサービス経由）

GitHubの`schedule`トリガーは高負荷時に遅延・間引きされるbest-effort機能のため
使用せず、無料の外部cronサービス（例: [cron-job.org](https://cron-job.org)）から
`workflow_dispatch` APIを定期的に叩く運用にしています。

1. `https://github.com/settings/personal-access-tokens/new` で、このリポジトリのみ
   ・`Actions: Read and write` 権限のfine-grained PATを発行
2. cron-job.orgで新規ジョブを作成し、以下を設定
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/scrape.yml/dispatches`
   - Method: `POST`
   - Headers:
     - `Authorization: Bearer <上記PAT>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Body: `{"ref":"main"}`
   - 実行間隔: 任意（例 30分おき）
3. PATには有効期限があるため、切れたら再発行してcron-job.org側のHeadersを更新する

この方式で起動した実行は、GitHub Actions画面上では`Scheduled`ではなく
`Manually run by ...`と表示されます（`workflow_dispatch`イベント扱いのため。
仕様通りで異常ではありません）。

### 5. 動作確認

Actions タブから `Satellite imagery scrape` を手動実行（Run workflow）してみて、
`satellite_out/satellite_imagery.geojson` が更新されコミットされることを確認して
ください。

## ローカルでの動作確認

```bash
pip install -r requirements.txt
export MGP_API_KEY=あなたのMGP_ProのAPIキー
python satellite_scraper.py --once --out-dir satellite_out
```

`geoplot-sat.html` をブラウザで直接開く（`file://`）と、既定で
`./satellite_out/satellite_imagery.geojson` への読み込みを試み、失敗した場合は
自動的にサンプルデータ（18件）にフォールバックします。

## 設計上の注意点・既知の制約

- **AOIポリゴンはv1の手作り簡易版**（`data/china_msa_aoi.geojson`）です。
  Natural Earth等の行政境界データを使った精緻化は未対応。近隣国・公海を意図的に
  広めに含めており、MGP側はbbox検索後にこのポリゴンでの中心点フィルタ、
  Copernicus側はこのポリゴンをそのままintersectsクエリに使っています。
- **間引きなしの全件累積**方式です。長期運用でリポジトリ・geojsonが肥大化する
  懸念は認識済みですが、対応（日付/地域でのファイル分割、古いデータのアーカイブ化等）
  は別途の検討事項として保留しています。
- **ダウンロードは外部リンク方式**です。ビューアのポップアップから元カタログ
  ページ（MGP Pro / Copernicus Browser）を新規タブで開き、その場でログインして
  ダウンロードしてもらう想定です。認証情報やアクセストークンをこのリポジトリ・
  静的サイト側に一切持たせないための選択です（画像サイズが大きい問題への対応も
  兼ねています。将来的にワンクリックDLにしたい場合は、Cloudflare Workers等の
  軽量サーバーレス関数を1つ追加してストリーム中継する方式が候補です）。
- MGP Discovery APIの`intersects`（任意ポリゴンでの検索）対応有無は契約により
  未確認のため、現状はbboxのみを使用しています。
- 各APIのレスポンス項目名は契約内容やAPIバージョンにより差異があり得ます。
  実データで挙動が合わない場合は`providers/mgp.py` / `providers/copernicus.py`の
  `normalize()`を調整してください。
- `commit_state.py`の`git reset --hard origin/<branch>`はCI専用の前提です。
  ローカル手元での実験用途には使わないでください。
