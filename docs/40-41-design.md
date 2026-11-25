# 機器 40・41 設計メモ

草案、2026-11-25（作成）／2026-11-26（決定の反映）。

- 機器 40（予測器）：毎日の summarize 直後に翌日の予測を記録し、翌日の結果が出たら点数を付ける。**決定済み、実装着手中。**
- 機器 41（増幅器）：毎日のニュース盤の候補を「確かめる価値の高い順」に並べ替える。**決定済み、実装はラベルが集まってから。**

## 共通の決まり

- 既存の `daily.yml` の中で動かす。新しい cron は足さない。
- 有料 API は使わない。ローカル LLM は後の段階で検討する（当面は規則だけ）。
- 較正が済むまで「未較正」と明示する。
- どちらも既存の考え方の組み合わせ。新規手法とは名乗らない。
- 予測と並べ替えの結果は、本体の `data/<ban>/*.json` を書き換えずに別ファイルに置く。
- **採点の前に、予測ファイルが前日の commit から変わっていないこと（git 上のファイルの指紋）を確かめ、変わっていたら採点せずに記録する。**

---

## 機器 40：予測器（決定版）

### 入出力

- 入力（当日の summarize の時点で揃うもの）
  - 各盤の過去 30 日分の `after_filter`, `records_delta`, `status`
  - 現在の `streaks.zero_filter`, `streaks.zero_new`
  - 稼働日カレンダー（`runner/summarize.py:is_jst_workday`）
- 出力（翌日 1 日分の予測）
  - `after_filter` の範囲 `[low, high]`（**80% 経験分位区間**）
  - `records_delta` の範囲 `[low, high]`（**80% 経験分位区間**）
  - 状態分布 `{ok: p1, warn: p2, ng: p3, stale: p4}`（和 = 1、Laplace スムージング）

### 台帳とファイル置き場

```
predictions/
  <YYYY-MM-DD>.json         ← target_date の予測（D-1 の summarize 直後に書く）
  <YYYY-MM-DD>-score.json   ← target_date の summarize 後に書く
status/history.jsonl        ← 盤・日ごとの status 時系列（1 盤 1 行/日、append）
```

- D-1 の daily commit に `<D>.json` を含める。**ファイルを後から書き換えない**（固定）。
- **`predictions/` は削除せず、ずっと残す**（`DATA_RETENTION_DAYS` の対象外、`prune_old_data` は `data/` しか見ない）。
- `<D>-score.json` は翌日 D の summarize の中で 1 回だけ生成。再実行しても同じ内容に。
- `status/history.jsonl` は 1 行 1 盤 1 日分、同じ日付の再実行は差し替えで idempotent。

### 指紋チェック（fingerprint check）

採点の前に `predictions/<today>.json` が前日の commit から変わっていないことを確認する：

- `git log --format=%H -- predictions/<today>.json` の行数が **1 でない** → 書き換え検出
- `git diff --quiet HEAD -- predictions/<today>.json` が **非ゼロ** → 作業ツリーで変更あり
- どちらか失敗したら採点せず `fingerprint_check.ok = false` と理由を score ファイルに書き、`per_ban` は空にする。

### 基準との比較

| 基準            | 中身                                              |
|-----------------|---------------------------------------------------|
| baseline-last   | 前日と同じ値を出す（範囲は ±0、状態は確率 1 で前日のもの） |
| baseline-med7   | 直近 7 日の中央値、範囲は min〜max、状態は最頻値 |

点数は盤ごと・基準ごとに **14 稼働日** の移動窓で記録。較正状態とは独立に基準比を朝の報告に出す。

### 点数の付け方

- 状態分布：Brier score  `Σ(p_i - 1_{i=actual})²`（3〜4 クラス合計を 1 日 1 盤で記録）
- 範囲：
  - `coverage_hit = 1 if low <= actual <= high else 0`
  - `miss = 0 if hit else min(|actual-low|, |actual-high|)`
  - **範囲に入った割合（hit_rate）と、それが 80% にどれだけ近いか（`proximity_to_80pct = 1 − |hit_rate − 0.8|`）** を記録
- 起動の遅れ：当面見送り（GitHub Actions の遅延はこれまで 4〜8 時間の幅が大きく、朝の報告には影響小、別タスクで扱う）

### 較正の目安（確定）

- **14 稼働日固定**。それ未満は `uncalibrated`。
- **基準に負け続けても止めない**。事実（勝ち／負け／引き分け）を出し続ける。
- 朝の報告には **1 行**：`較正日数 N / vs baseline-last=... / vs baseline-med7=... / hit_rate=...（proximity=...）/ status=calibrated|uncalibrated`

### 表示先

朝の報告の各盤の行の末尾に 1 行だけ追加。盤目録 (`mokuroku.json`) の `alert_count` 計算は触らない。

### 既存手法との関係

- 持続性ベースライン（前日と同じ）は気象予報の null model。
- Brier score は 1950 年の多クラス確率予測の定番。
- 経験分位で区間を作る方法は conformal prediction の最単純形。
- 移動窓評価は時系列予測の基本。

これらの組み合わせで、新規性は無い。

### 動く場所

- `.github/workflows/daily.yml` の summarize ジョブの中で、`runner/forecast.py` を 1 回呼ぶ。新規 cron なし。
- 出力ファイルは既存の `git add status data` と同じ step で commit に含める（`predictions/` は明示追加）。

---

## 機器 41：増幅器（決定版、設計のみ）

### 入出力

- 入力：当日の `data/news-ban/<日付>.json` の `records`（24 件前後、重複除去済み、時系列降順）
- 出力：
  - 件ごとに `rank_score`（整数、高いほど確かめる価値が高いと見積もる）と `rank_reasons`（効いた規則名の配列）
  - 並べ替え後の順序（rank_score 降順、同点時は元の順序）
- 本体 `records` は触らない。別ファイルに書く。

### 台帳とファイル置き場

```
data/news-ban/
  <YYYY-MM-DD>.json         ← 本体。触らない
  <YYYY-MM-DD>-amp.json     ← 並べ替え後 [{id, url, rank_score, rank_reasons}]
labels/
  <YYYY-MM-DD>.jsonl        ← 本人のラベル（週末まとめて付ける）
labels/sampled/
  <YYYY-MM-DD>.jsonl        ← ラベルを付ける候補、各日 10 件、無作為抽出
```

### ラベル付けの運用（確定）

- ラベルは**本人のみ**、**週末にまとめて**付ける。
- 粒度：**2 値**（価値高 = 1 / 普通 = 0）。
- 1 日あたり **10 件を無作為抽出**して `labels/sampled/<日付>.jsonl` に書き出す。増幅器の順位・点数は見せない形で並べる（上位だけにラベルを付けると recall が測れないため）。
- 本人は `labels/sampled/<日付>.jsonl` を見てラベルを `labels/<日付>.jsonl` に書く。

### 始めの規則（無料・ローカル判定のみ）

| 名前              | 当たるとき                                              | 点 |
|-------------------|---------------------------------------------------------|----|
| R1 numbers        | タイトルに数字を含む（年号・日付以外。件数・金額・割合など） | +1 |
| R2 claim-shape    | 「と発表」「と公表」「ことが分かった」「明らかにした」等 | +1 |
| R3 measurement    | 件・円・人・％・倍・名・kg・km など測定語                | +1 |
| R4 identifier     | 条文番号・命令番号・事件番号・識別子                     | +1 |
| ~~R5 source-tier~~ | （**今は作らない**。将来必要になったら別途）           | — |

### 較正の方法（確定）

1. 本人が `labels/<日付>.jsonl` に「確かめる価値高 = 1 / 普通 = 0」を書く。
2. **develop は始めた月、holdout はその翌月**（例：11 月に始めたら 12 月を holdout にする）。
3. 規則を書くときに holdout の期間は見ない。
4. 評価の指標：holdout のラベル 1 がある日の **top-5 リコール**（`recall@k`, **k = 5**）を、date 降順の baseline と並べて比較する。
5. 較正済み後も「holdout の recall@5 が baseline を上回っている」状態を保てる間だけ、朝の報告で並べ替え結果を出す。
6. **規則を書き換えたら、holdout を新しく取り直す**（以前の holdout は使わない）。

### 既存手法との関係

- 手作り特徴の加算スコア（BM25 以前からある古典的 IR 手法）
- holdout による評価（機械学習の標準）
- 一次出典・数字・主張の形 — 事実確認（IFCN など）で広く共有されているヒューリスティック

新規性は無い。

### 動く場所

- `runner/amplify.py` を summarize の前（または後）に 1 回呼ぶ。新規 cron なし。
- ラベル付けは本人の週末作業、CI 側は候補の抽出のみを自動で行う。
- 出力ファイルは既存の `git add status data` に含める（`data/news-ban/*-amp.json`、`labels/sampled/*.jsonl`）。

---

## 開始条件

| 機器 | 始めるのに必要なもの                     | 較正の目安                              |
|------|------------------------------------------|-----------------------------------------|
| 40   | 既存の 30 日分の履歴（概ね揃っている）   | 盤ごとに 14 稼働日の点数（勝ち負けは別勘定で出し続ける） |
| 41   | 本人のラベル 30 件以上 / 14 稼働日分以上 | develop（始めた月）・holdout（翌月）の分割完了 + recall@5 で baseline 超え |

機器 40 は履歴がすでに揃うので、2026-11-26 に実装着手。
機器 41 は設計メモの更新のみ、実装はラベル付けの運用が実際に回り始めてから。

---

## 残っている判断待ち

### 機器 40

- [ ] `predictions/` は Git LFS を使うか否か（JSON のサイズは小さいので当面不要の見込み）
- [ ] 起動の遅れの予測を後から足すか（現状は見送り）

### 機器 41

- [ ] 1 日 10 件という抽出の件数は固定か、ニュース盤の候補数で変えるか
- [ ] `labels/sampled/` の生成は summarize 後の仕事として自動で作るか、本人が週末だけ作るか

---

草案終わり。機器 40 の実装は本ファイルと同時に進める。
