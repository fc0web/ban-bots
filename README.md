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

## 盤を増やす手順

1. `registry/bans.csv` に1行足す。`id` は既存と重複させない。
2. 必要なら、その盤の `source` が初出なら `sources/<種類>/fetch.py` を追加する（既存の `arxiv` / `zenodo` / `selfgen` / `none` で足りるなら不要）。
3. `enabled=true` にして push する。次の定時実行で取得が始まる。

**盤を増やすだけなら、ワークフローも Python コードも触らない。**

## 状態ファイルの形

`status/<分担番号>.jsonl` は 1盤1行の JSON Lines：

```json
{"ban":"<id>","checked_at":"<JST +09:00>","records":<数>,"records_delta":<前回との差>,"matched":<数>,"mismatch":<数>,"source_ok":true,"broken_links":<数>,"license_changed":false,"note":""}
```

- `checked_at` は `TZ=Asia/Tokyo date` の実測値。推測しない。
- 失敗しても必ず `source_ok:false` の行を書く（「空のまま成功」を防ぐ）。

## まとめ `status/summary.json`

```json
{"generated_at":"<JST>","total":<全盤数>,"ran_today":<今日回した数>,"ok":<数>,"warn":<数>,"ng":<数>,"stale":<数>,"problems":[...]}
```

- `ng`: `mismatch>0` または `source_ok=false` または `license_changed`
- `stale`: 予定の 1.5 倍以上更新がない盤
- `warn`: リンク切れあり、または毎日の盤で `records_delta=0`
- `problems` は注意・要対応・stale の盤だけ、最大 50 件。超えた分は件数だけ。

## 実行時刻

毎日 **07:27 JST** に取得が始まり、**07:50 JST までに** `status/summary.json` が更新される（Claude のまとめタスクが 08:37 に読む前に終わる）。

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
