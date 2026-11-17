# ban-bots

盤（ban）自動更新ボットの土台。盤が1万個以上に増えても回る作り。AI は使わず、Python 標準ライブラリと GitHub Actions（公開リポジトリなので無料）で動く。

## 考え方

- 盤は「プログラム」ではなく「設定表 (`registry/bans.csv`) の1行」。
- ボットは「元データの種類」ごとに1つだけ (`sources/<種類>/fetch.py`)。
- 盤ごとにワークフローは作らない。共通のランナー (`runner/run.py`) が設定表を読んで順番に処理する。
- 報告は「異常だけ」。正常な盤は数だけ数える（`status/summary.json`）。
- 通知は出さない（報告は Claude のスケジュール済みタスクが `summary.json` を読んで行う）。

## ディレクトリ構成

```
registry/bans.csv                 盤の設定表（1盤＝1行）
sources/<種類>/fetch.py           元データの種類ごとのボット
runner/run.py                     設定表を読み、担当の盤を順番に処理する共通の実行役
runner/summarize.py               全盤の状態を集めて summary.json を作る
data/<盤id>/YYYY-MM-DD.json       取得結果（説明用メタデータのみ）
status/<分担番号>.jsonl           その分担が処理した盤の状態（1盤＝1行）
status/summary.json               全盤まとめ（小さく保つ）
.github/workflows/daily.yml       毎日 07:2x JST ごろに回す
```

## 設定表 `registry/bans.csv` の列

| 列         | 意味 |
|------------|------|
| id         | 盤の識別子（英数字・ハイフン）。データ・状態のパスに使う。 |
| name_ja    | 日本語名 |
| name_en    | 英語名 |
| source     | 元データの種類。`+` で複数指定可（例: `arxiv+zenodo`）。`selfgen`・`none` も可。 |
| params     | 取得条件の JSON。`source` ごとのサブキーを持つ（例: `{"arxiv":{...},"zenodo":{...}}`）。 |
| freq       | `daily` / `weekly` / `monthly` |
| weekday    | `weekly`・`monthly` の曜日（`mon`-`sun`、`monthly` は「第1月曜」などではなく「その曜日の最初の週」）。`daily` では空。 |
| enabled    | `true` / `false` |
| owner      | 担当タブ名（運用メモ） |
| page_url   | 盤の公開ページ URL（あれば） |

## 使える元データの種類 (`source`)

| source     | 役割                                  | params の例 |
|------------|---------------------------------------|-------------|
| `arxiv`    | arXiv API（math.HO など）             | `{"category":"math.HO","limit":20}` |
| `zenodo`   | Zenodo REST API                       | `{"q":"mathematics education","limit":10}` |
| `pubmed`   | NCBI E-utilities（esearch+esummary）  | `{"queries":[{"q":"astrology","limit":5},{"q":"Barnum effect","limit":5}]}` または `{"q":"astrology","limit":5}` |
| `rss`      | 任意の RSS 2.0 / Atom フィード        | `{"feeds":["https://..."],"limit_per_feed":10,"include_keywords":["霊感","占い"]}` |
| `selfgen`  | 自作の生成器（placeholder）           | `{"kind":"placeholder"}` |
| `none`     | 何もしない（目録・監視など）          | `{}` |

複数の元データを 1 盤で使うときは `+` で繋ぐ：`source=arxiv+zenodo` / `source=pubmed+rss`。この場合 `params` は `{"arxiv":{...},"zenodo":{...}}` のようにソース名でキー分けする。

**候補のみ保存。選ぶ・言い換えるはしない（Claude 側の仕事）。** `pubmed` と `rss` の各レコードには `mark: "未測"` が付く（評価していないことを表す）。

**rss の `include_keywords`**：題名または `<description>` / `<summary>` にいずれかの語を含むアイテムだけを候補に残す。空リストなら絞り込み無し（素通し）。日本語はそのまま部分一致、英語は大文字小文字を無視する。`<description>` / `<summary>` は絞り込み判定にのみ使い、候補レコードには保存しない。

**占い盤（uranai）の RSS フィード選定メモ**：
- 消費者庁 (`caa.go.jp`) は公式 RSS を公開しているので採用 → `https://www.caa.go.jp/news.rss`（RSS 2.0、dc:date 付き、消費者庁ウェブサイト「RSSフィードについて」（`https://www.caa.go.jp/rss/`）で案内されている）。
- 国民生活センター (`kokusen.go.jp`) は公式 RSS フィードが見当たらなかった（トップページから案内がなく、`/rss/`、`/rss.xml`、`/feed/` はいずれも 404。トップに `info_rss.jpg` 画像はあるが、リンク先は HTML 一覧 `/new/new.html`）。採用しない。見つかり次第 `feeds` に 1 本足せばよい。

**ニュース盤（news-ban）・広告盤（kokoku-ban）の情報源の候補（案 — 要確認）**：

ページは配置済み（`/tools/news-ban/`・`/tools/kokoku-ban/`）、`registry/bans.csv` には行だけ入れて `source=none / enabled=false` のまま置いてある。下の表は 2 盤に充てる情報源の **案**。**藤本さんの確認が済むまで `registry/bans.csv` の params は書き換えない**。URL の生死は未確認のものには「要確認」と記す（実装前に実際に GET して XML パースまで通ることを確認する）。

ニュース盤（news-ban）：報道（主張の出どころ）と 一次出典（照合用）を分けて集める。実装はまず既存の `rss` ソースでまかなえる。

| 種別 | 情報源 | 候補 URL | 想定ソース | 確認状況 |
|------|--------|---------|-----------|---------|
| 一次 | 消費者庁 新着 | `https://www.caa.go.jp/news.rss` | `rss` | 確認済み（uranai で稼働中） |
| 一次 | 気象庁 XML 電文 | `https://www.data.jma.go.jp/developer/xml/feed/regular.xml` | `rss`（Atom） | 要確認 |
| 一次 | 公正取引委員会 新着 | `https://www.jftc.go.jp/` | `rss` | 要確認（公式 RSS の有無） |
| 一次 | e-Stat 政府統計 API | `https://www.e-stat.go.jp/api/` | **新ソース `estat`** | 要確認（API キー取得と無料枠） |
| 報道 | Google News 日本 topic feed | `https://news.google.com/rss?hl=ja&gl=JP` | `rss` | 要確認（利用規約と件数） |
| 報道 | NHK 報道発表 | `https://www.nhk.or.jp/` | `rss` | 要確認（公式 RSS の有無） |

`include_keywords` 案：news-ban では空（事実系の主張は分野が広く、キーワード絞りは種類ラベルで行う）。

広告盤（kokoku-ban）：広告の手法と表現の **図鑑**。ルール違反事例・景表法事例・消費者注意喚起を候補として集め、人の確認で「手法」ラベルを付ける運用。`rss` の `include_keywords` で広告関連だけに絞る。

| 種別 | 情報源 | 候補 URL | 想定ソース | 確認状況 |
|------|--------|---------|-----------|---------|
| 一次 | 消費者庁 新着 | `https://www.caa.go.jp/news.rss` | `rss` | 確認済み（uranai 既出、別フィルタで再利用） |
| 一次 | 公正取引委員会 新着 | `https://www.jftc.go.jp/` | `rss` | 要確認 |
| 一次 | JARO（日本広告審査機構） | `https://www.jaro.or.jp/` | `rss` | 要確認（審議案件 RSS の有無） |
| 一次 | 日弁連 消費者問題 | `https://www.nichibenren.or.jp/` | `rss` | 要確認 |
| 一次 | 厚労省 医薬品等広告 | `https://www.mhlw.go.jp/` | `rss` | 要確認 |

`include_keywords` 案：`["広告","景品表示法","不当表示","誇大","ステマ","優良誤認","有利誤認","措置命令"]`。

次の手順（確認後）：
1. 「要確認」の各 URL を実 GET + パース検証（`sources/rss/fetch.py` に通す）。生きている分だけ採用。
2. `estat` を採るなら `sources/estat/fetch.py` を追加。
3. `registry/bans.csv` の news-ban / kokoku-ban 行の `source` と `params` を書き換える（`none` → `rss` もしくは `rss+estat`）。同じ行で `enabled=true` にする。

## 盤を増やす手順

1. `registry/bans.csv` に1行足す。`id` は既存と重複させない。
2. 必要なら、その盤の `source` が初出なら `sources/<種類>/fetch.py` を追加する（既存の `arxiv` / `zenodo` / `selfgen` / `none` で足りるなら不要）。
3. `enabled=true` にして push する。次の定時実行で取得が始まる。

**盤を増やすだけなら、ワークフローも Python コードも触らない。**

## 状態ファイルの形

`status/<分担番号>.jsonl` は 1盤1行の JSON Lines：

```json
{"ban":"<id>","checked_at":"<JST +09:00>","records":<新着数>,"records_delta":<新着数>,"matched":<数>,"mismatch":<数>,"source_ok":true,"broken_links":<数>,"license_changed":false,"note":"","raw_fetched":<元データから取った総数>,"after_filter":<絞り込み後の件数>}
```

- `checked_at` は `TZ=Asia/Tokyo date` の実測値。推測しない。
- 失敗しても必ず `source_ok:false` の行を書く（「空のまま成功」を防ぐ）。
- **`records_delta` の意味**：**その日に初めて入った件数**（＝新着）。日をまたいで ID（pubmed なら PMID、rss なら guid／URL、arxiv/zenodo なら各ソースの id）で重複排除する。前日までに入ったものは候補に数えない。`records` も同じ数（＝その日の新着＝データファイルに保存される候補数）。
- `raw_fetched`：絞り込みも重複排除も無しに元データから取った総数。
- `after_filter`：キーワード絞り込み（rss の `include_keywords`）後の件数。rss 以外や、絞り込み無しのときは `raw_fetched` と同じ値。
- データファイルは「その日に初めて入った候補」だけ保存し、以前の日に既に入ったものは再掲しない（一度 git history に残れば十分）。

**ID 重複排除の仕組み**：盤ごとに `data/<盤id>/_seen.json` を持ち、{id: 初めて入った日} の形で記録する。TTL 730 日で自動的に古いものを落とす。

## まとめ `status/summary.json`

```json
{"generated_at":"<JST>","total":<全盤数>,"ran_today":<今日回した数>,"ok":<数>,"warn":<数>,"ng":<数>,"stale":<数>,"problems":[...]}
```

- `ng`: `mismatch>0` または `source_ok=false` または `license_changed`
- `stale`: 予定の 1.5 倍以上更新がない盤
- `warn`: リンク切れあり、または毎日の盤で `records_delta=0`
- `problems` は注意・要対応・stale の盤だけ、最大 50 件。超えた分は件数だけ。

## 実行時刻と無人実行について

毎日 **07:27 JST** に GitHub Actions の cron で取得が始まる。Claude のまとめタスクが読むのは **08:37 JST**。

**この定期実行はサイト自動更新ボットの例外として本人（fc0web）承認済みで、クラウド無人実行が前提。** 人間が張り付かなくても回る。異常時の通知は出さず、`status/summary.json` と `status/mokuroku.json` に状態を残すだけ。読むのは Claude のまとめタスクの役目。

時刻はすべて `TZ=Asia/Tokyo date` の実測値を使う（推測しない）。直近の手動実行（workflow_dispatch）の計測値：

| 項目                         | 実測         |
|------------------------------|--------------|
| 起動                         | 09:12:09 JST |
| 完了（コミット push まで）    | 09:12:35 JST |
| 所要時間                     | 26 秒        |

cron 起動時刻 07:27 JST + GitHub Actions の起動遅延（通常 0〜5 分）＋ 本処理 26 秒 → 遅くとも **07:33 JST ごろ** には `summary.json` が更新済み。08:37 JST の読み取りまで 60 分以上の余裕がある。

## データ保持期間

`data/<盤id>/YYYY-MM-DD.json` は **直近 30 日分のみ** 残す。31 日以上前のファイルは `runner/summarize.py` が毎回の実行時に削除する。盤が 1 万個に増えてもリポジトリが太り続けないようにするため。

- 盤 1 件あたりの保存量: 最大 30 ファイル
- 削除対象: ファイル名 `YYYY-MM-DD` が今日から 29 日より前
- 復旧: 必要なら git の履歴から取り出せる（削除も履歴に残るため）

## mokuroku（目録＋監視）の表示

毎回の実行で `status/mokuroku.json` が再生成される。盤全体の状態を一枚にまとめた表示用ファイル：

- `ng` または `stale` が 1 件でもあれば `"color": "red"` と `"alert": true`
- どちらも 0 なら `"color": "green"` と `"alert": false`
- 問題盤の一覧（最大 50 件）も同時に載る

mokuroku を表示する側（Claude のまとめタスクなど）は、この `color` / `alert` を見て赤く出す。

## 手で動かす

```bash
# 1 シャード分（シャード 0 / 全 1 シャード）を手元で動かす
python runner/run.py --shard 0 --total 1

# まとめを作る
python runner/summarize.py
```

## 守ること

- 有料サービス・有料 API を使わない。
- 秘密情報（トークン等）をリポジトリに書かない。
- 新しい盤は「設定表に1行足す」だけで増やせる。
