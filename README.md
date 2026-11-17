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

**ニュース盤（news-ban）・広告盤（kokoku-ban）の情報源の候補（確認済み／不可）**：

方針（2026-11-17 確認後）：
- ニュース盤は **官公庁の一次発表のみ**。報道（Google News・NHK など）は見出しをそのまま保存する運用になり、ページの決まり「見出しを写さない」とぶつかるため **採用しない**。
- 気象庁 XML・e-Stat は **候補の収集源には使わず**、**後で照らし合わせに使う候補**として下に残す（人の確認が付いたときに手で参照する運用）。
- 広告盤は、公取委・厚労省・JARO・日弁連の RSS を実検証し、**生きているもののみ**採用。

ニュース盤（news-ban）：候補収集用（`source=rss`、`include_keywords=[]`）

| 種別 | 情報源 | 確認済み RSS URL | 実測 raw | 備考 |
|------|--------|-----------------|---------:|------|
| 一次 | 消費者庁 新着 | `https://www.caa.go.jp/news.rss` (RSS 2.0+dc) | 20 | uranai で既出・稼働中 |
| 一次 | 厚労省 新着 | `https://www.mhlw.go.jp/stf/news.rdf` (RSS 1.0/RDF) | 102 | channel dc:date は古いが item は現行 |
| 一次 | 内閣府 報道発表新着 | `https://www.cao.go.jp/rss/news.rdf` (RSS 1.0/RDF) | 10 | 景気動向指数・GDP 速報など |
| 一次 | 総務省 新着 | `https://www.soumu.go.jp/news.rdf` (RSS 1.0/RDF, Shift_JIS) | 20 | パーサに Shift_JIS 対応を追加済み |

**取得不可**（当該ドメインが bot UA を 403 で拒否するので採用しない）：
- 公正取引委員会 (`https://www.jftc.go.jp/`) — 403 Forbidden。browser UA へのなりすましは行わない。
- 経済産業省 (`https://www.meti.go.jp/`) — 同上。

ニュース盤の `include_keywords` 案：**空**（事実系の主張は分野が広いため、絞りはページ側の「種類」ラベルで行う）。

後で照らし合わせに使う候補（README に残すのみ、registry には入れない）：
- 気象庁 XML 電文: `https://www.data.jma.go.jp/developer/xml/feed/regular.xml`（Atom、災害・天気予報の一次照合用）
- e-Stat 政府統計: `https://www.e-stat.go.jp/api/`（統計 API、数字の一次照合用。API キー取得と無料枠要確認）

---

広告盤（kokoku-ban）：候補収集用（`source=rss`、キーワード絞り込み）

| 種別 | 情報源 | URL | 確認結果 |
|------|--------|-----|---------|
| 一次 | 消費者庁 新着 | `https://www.caa.go.jp/news.rss` | 生、raw 20 件/日 |
| 一次 | 厚労省 新着 | `https://www.mhlw.go.jp/stf/news.rdf` | 生、raw 102 件（上限 50 で切り詰め） |
| 一次 | 内閣府 新着 | `https://www.cao.go.jp/rss/news.rdf` | 生、raw 10 件/日 |
| 一次 | 総務省 新着 | `https://www.soumu.go.jp/news.rdf` | 生、raw 20 件/日 |
| 一次 | 公正取引委員会 | `https://www.jftc.go.jp/` | **死**（403 Forbidden、bot UA 拒否）— 採用不可 |
| 一次 | JARO | `https://www.jaro.or.jp/` | **RSS 無し**（公式 RSS/Atom なし。`/rss/`, `/rss.xml`, `/feed/` すべて 404） |
| 一次 | 日弁連 | `https://www.nichibenren.or.jp/` | **RSS 無し**（同上） |

`include_keywords` 案（2026-11-17 追加分含む）：
```
["広告", "景品表示法", "不当表示", "誇大", "ステマ", "優良誤認", "有利誤認", "措置命令",
 "課徴金", "打消し表示", "定期購入", "特定商取引法"]
```

本日のキーワード絞り込み実測（4 フィード × 上記 12 語）：
- CAA: raw 20 → kw 0
- MHLW: raw 102（上限 50 採用）→ kw 0
- CAO: raw 10 → kw 0
- MIC: raw 20 → kw **1**（総務省の 1 件が「広告」を含む）
- 合計: 本日は **1 件**だけ候補通過。

広告盤は、広告規制の本家（公取委・JARO・日弁連）がボット経路で届かない結果、採用可能なのは上記 4 フィードのみ。raw ベースの流量は高いが、広告系キーワード絞り込み後の通過件数は日によって 0〜数件に収まると見込まれる。無理に広げるより、「少なく確実」の運用にしてから後で広げる方針。

---

書き換え方針（藤本さん OK のあと）：

- **ニュース盤**:
  ```
  source  = rss
  params  = {"feeds": [CAA, MHLW, CAO, MIC の 4 URL], "limit_per_feed": 10, "include_keywords": []}
  enabled = true
  ```
- **広告盤**:
  ```
  source  = rss
  params  = {"feeds": [CAA, MHLW, CAO, MIC の 4 URL], "limit_per_feed": 20, "include_keywords": [上記 12 語]}
  enabled = true
  ```
- `sources/rss/fetch.py` に RSS 1.0 (RDF) 対応と Shift_JIS 付き XML 宣言の対応を追加済み（この commit に同梱）。この変更は uranai にも影響するが、既存の CAA 読み取りは退行なし（regression test 済み: raw 20, kw 0）。

## ページへの候補反映（設計案 — まだ実装しない）

ニュース盤・広告盤のページ（`https://rei-aios.pages.dev/tools/news-ban/` / `/kokoku-ban/`）が ban-bots のデータファイル（`data/<盤id>/YYYY-MM-DD.json`）を読んで、ページ内の **「未確認（候補）」欄** に並べる案。

**本実装の際に決めるスコープ外のルール（忘れないよう先に書く）**：
- 候補はページの「主張の答え合わせ」欄には入れない。**別の「未確認」欄**として並べる。
- 架空の見本データ（`DEMO=true`）は、本物の候補が入った時点で外す（データファイルが空でない日は demo を表示しない、など）。

### (A) ページから直接 `raw.githubusercontent.com` を読む（client-side fetch）

```js
const url = `https://raw.githubusercontent.com/fc0web/ban-bots/main/data/news-ban/${today}.json`;
const data = await fetch(url).then(r => r.json());
// data.records を「未確認」欄にレンダリング
```

**利点**：
- インフラ追加なし。rei-aios / ban-bots の両方で CI 変更不要。
- 常に ban-bots の最新 commit を読む（GitHub 側 CDN キャッシュ ~5 分）。
- 日付ファイル名を URL に埋めるだけで、特定の日の履歴にも遡れる。

**欠点**：
- `raw.githubusercontent.com` への外部依存（GitHub の rate limit・CDN 仕様変更に左右される）。
- GitHub が落ちるとページから候補が見えなくなる。
- ban-bots リポジトリを非公開に切り替えた瞬間に壊れる（公開前提を URL に埋めてしまう）。
- 日付欠落（CI 失敗日など）や JST 日付境界の処理を全部クライアント側 JS で書く必要がある。

### (B) ban-bots → rei-aios ミラー（cross-repo コピー、同一オリジン配信）**【推奨】**

ban-bots の daily workflow の最後に `public/tools/news-ban/data/` と `public/tools/kokoku-ban/data/` の下に `.json` を push（rei-aios リポジトリへ）。rei-aios の CF Pages が静的ファイルとして配信。ページは `fetch('./data/YYYY-MM-DD.json')` の相対パスだけで読む。

**利点**：
- ページと同一オリジン（CORS の心配なし）。
- CF Pages の CDN に乗る（高速・安定）。
- rei-aios の git 履歴にデータが残り、版管理・復元が容易。
- ページ側コードは相対パスだけで、特定リポジトリ URL に縛られない。
- rei-aios の build に乗せれば inline 埋め込みも選べる。

**欠点**：
- cross-repo push の権限（PAT もしくは GitHub App token）が必要。secret 管理が増える。
- rei-aios の git ツリーが日次で少しずつ肥大（1 日分 10〜50KB。ban-bots の 30 日 retention と揃えて rei-aios 側も prune する運用が要る）。
- 既に多タブで活発に commit されている rei-aios に新しい自動 commit 元を足す。tab isolation protocol との整合を確認。
- 反映遅延 = ban-bots 実行 + ミラー sync + CF Pages deploy（通常 3〜5 分）。

### (C) Cloudflare Pages Functions / Workers のサーバー側プロキシ

rei-aios の `functions/api/candidates/[ban].ts` などに関数を置き、ページは同一オリジンの `/api/candidates/news-ban?date=...` を叩く。関数内で ban-bots の raw または GitHub Content API を読む。

**利点**：
- 同一オリジン（CORS なし）。
- edge で短期キャッシュ可能。
- サーバー側で変換（demo 除去・「未確認」用整形）をかけてから返せる。
- rei-aios のリポジトリにデータファイルを置く必要がない（軽い）。

**欠点**：
- CF Pages Functions の運用（node runtime 制約、環境変数、secrets）。
- 上流は結局 `raw.githubusercontent.com` なので、(A) と同じ外部依存を内側に隠しただけ。
- 設定・デバッグの手間が (A)(B) より重い。

### 推奨：**(B) ミラー方式**

同一オリジン・CDN 配信・git 履歴・単純な fetch コードが揃うため。本実装時に別途相談する項目：
- cross-repo push の権限管理（PAT or GitHub App、scope）
- rei-aios 側の保存場所（`public/tools/<ban>/data/YYYY-MM-DD.json`、retention prune も同梱）
- demo の外し方（データファイル空のときのみ demo を見せる、など）
- 「未確認」欄のデザイン（「主張の答え合わせ」と視覚的に分離、`mark: "未測"` をラベル化）

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
