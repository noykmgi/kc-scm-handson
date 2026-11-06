# 日用品 SCM 需給調整会議 業務引き継ぎノート（データ集計ルール・暗黙知まとめ）

最終更新: 2026-10-01
作成部署: SCM戦略部 需給計画グループ

本ドキュメントは、月次の需給調整会議における「販売計画 vs 出荷実績（予実乖離）」の集計時に、基幹システム（SAP 由来テーブル）および計画系システムのデータを正しく結合・計算するための社内業務ルールをまとめたものです。

---

## 1. 対象テーブルとカラムの業務的意味 (`kc_handson_scm`)

### 1.1 品目マスタ (`ZMATMST`)
* `MATNR` (STRING): 品目コード（主キー。例: `M001` 〜 `M010`、旧品番 `M903`）。
* `MAKTX` (STRING): 品目名（商品名・容量）。
* `CAT_CD` (STRING): 商品カテゴリコード（`ORAL`, `LAUNDRY`, `KITCHEN`, `HAND`）。
* `UMREZ` (INT64): **ケース入数（1ケースあたりのバラ個数）**。品目ごとに `12`, `24`, `48` と異なる。
* `ZZOLD_MAT` (STRING, NULLABLE): **リニューアル前の旧品番コード**。パッケージ変更等で品番が切り替わった新品番のレコードには、ここに旧品番（例: `M903`）が格納されている。

### 1.2 販売計画テーブル (`ZPLAN_BUILDUP`)
* `SPMON` (STRING): 計画対象年月（`YYYY-MM` 形式。例: `'2026-09'`）。
* `MATNR` (STRING): 品目コード（※リニューアル品目の場合、計画は新品番側にのみ登録されている）。
* `VERSN` (STRING): **計画バージョン区分コード**。以下の 3 種類が存在する：
  * `'V0'`: 営業部門の積上げ素案（未承認）
  * **`'V1'`: 月初確定販売計画（公式の予実評価基準！）**
  * `'V9'`: 月末時点の着地見込み（ローリングフォーキャスト。実績値に近い見通しであり、計画値ではない）
* `PLNMG` (INT64): **計画数量（単位：バラ個数 / Pieces）**。

### 1.3 出荷実績テーブル (`ZSHIP`)
* `WADAT` (DATE): 出荷計上日（月次集計時は `FORMAT_DATE('%Y-%m', WADAT)` で `SPMON` と合わせる）。
* `MATNR` (STRING): 出荷された品目コード（切替月は旧品番での出荷明細も含まれる）。
* `WERKS` (STRING): 出荷拠点プラントコード（`W01`=東日本物流センター, `W02`=西日本物流センター）。
* `LFART` (STRING): **出荷伝票タイプ（社内アドオン区分）**。以下の 3 種類が存在する：
  * **`'ZL01'`: 得意先向け実売出荷（販売計画と比較する唯一の集計対象！）**
  * `'ZL02'`: 東西倉庫間の在庫横持ち転送（売上・実売ではないため集計対象外）
  * `'ZSMP'`: 店頭試供品・展示サンプル無償出荷（集計対象外）
* `LFIMG` (INT64): **出荷数量（単位：ケース数 / Cases）**。※バラ個数ではない！

---

## 2. 予実乖離集計における「4つの必須業務ルール」

1. **【ルール①：計画バージョンは必ず `VERSN = 'V1'` を採用する】**
   * `VERSN = 'V9'` は月末着地見込みであり、予実乖離の評価に使ってはならない。また全バージョンの合算も厳禁。必ず **`VERSN = 'V1'`（月初確定計画）** で絞り込むこと。
2. **【ルール②：出荷伝票タイプは必ず `LFART = 'ZL01'` のみに絞る】**
   * 倉庫間横持ち（`'ZL02'`）や無償サンプル（`'ZSMP'`）を含めると出荷実績が過大計上される。必ず **`LFART = 'ZL01'`** のみを集計すること。
3. **【ルール③：出荷数量は `LFIMG * UMREZ` でバラ個数に換算する】**
   * `ZSHIP.LFIMG` はケース単位、`ZPLAN_BUILDUP.PLNMG` はバラ個数単位である。出荷テーブルの品目（`s.MATNR`）に対して品目マスタの `UMREZ` を掛け、**`SUM(s.LFIMG * m_ship.UMREZ)`** でバラ個数に換算すること。
4. **【ルール④：リニューアル品目は旧品番（`ZZOLD_MAT`）の出荷実績を新品番へ合算する】**
   * `ZMATMST.ZZOLD_MAT` に値がある新品番（例: `M003` の旧品番 `M903`）については、旧品番で出荷された実売個数も新品番の出荷実績に合算してから `VERSN = 'V1'` の計画と比較すること。

---

## 3. 予実乖離率の公式定義と検証済み SQL パターン

* **予実乖離率(%)**: `ROUND(SAFE_DIVIDE(実売出荷個数 - 確定計画個数, 確定計画個数) * 100, 1)`
* **乖離ワースト/トップ上位の抽出順**: プラス乖離（欠品リスク）とマイナス乖離（過剰在庫リスク）の双方を検知するため、**乖離率の絶対値 `ABS(...)` の降順** でランキングする（社内要注意閾値は `±20%` 以上）。

```sql
WITH confirmed_plan AS (
  SELECT
    p.MATNR,
    m.MAKTX,
    m.ZZOLD_MAT,
    p.PLNMG AS plan_pcs
  FROM `kc_handson_scm.ZPLAN_BUILDUP` AS p
  JOIN `kc_handson_scm.ZMATMST` AS m
    ON p.MATNR = m.MATNR
  WHERE p.SPMON = '2026-09'
    AND p.VERSN = 'V1'
),
ship_by_mat AS (
  SELECT
    s.MATNR,
    SUM(s.LFIMG * m.UMREZ) AS ship_pcs
  FROM `kc_handson_scm.ZSHIP` AS s
  JOIN `kc_handson_scm.ZMATMST` AS m
    ON s.MATNR = m.MATNR
  WHERE FORMAT_DATE('%Y-%m', s.WADAT) = '2026-09'
    AND s.LFART = 'ZL01'
  GROUP BY s.MATNR
)
SELECT
  cp.MATNR,
  cp.MAKTX,
  cp.plan_pcs,
  COALESCE(s_new.ship_pcs, 0) + COALESCE(s_old.ship_pcs, 0) AS actual_pcs,
  ROUND(
    SAFE_DIVIDE(
      (COALESCE(s_new.ship_pcs, 0) + COALESCE(s_old.ship_pcs, 0)) - cp.plan_pcs,
      cp.plan_pcs
    ) * 100,
    1
  ) AS deviation_pct
FROM confirmed_plan AS cp
LEFT JOIN ship_by_mat AS s_new
  ON cp.MATNR = s_new.MATNR
LEFT JOIN ship_by_mat AS s_old
  ON cp.ZZOLD_MAT = s_old.MATNR
ORDER BY ABS(deviation_pct) DESC;
```
