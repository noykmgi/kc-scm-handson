"""Knowledge Catalog & Knowledge Studio ハンズオン用:
ミニ SCM データセット（BigQuery 3テーブル）の生成・投入および初期シード登録スクリプト。

仕込まれている4つの業務ルール（生データ覗き見だけではAIが解けないリアルな企業暗黙知）:
  1. 計画バージョン区分 (`VERSN`): 'V0'(素案), 'V1'(月初確定計画=評価基準!), 'V9'(月末着地見込=実績近似値)
  2. 出荷伝票タイプ (`LFART`)    : 'ZL01'(得意先実売出荷=集計対象!), 'ZL02'(倉庫間横持ち転送=除外), 'ZSMP'(無償サンプル=除外)
  3. 単位換算 (`LFIMG` × `UMREZ`): 出荷 `LFIMG` はケース数、計画 `PLNMG` はバラ個数
  4. リニューアル新旧合算 (`ZZOLD_MAT`): 新品番 `M003` は旧品番 `M903` の実売出荷を合算して評価（合算すると乖離 0.0%）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time

from google.auth.transport.requests import AuthorizedSession
from google.cloud import bigquery

from inspect_mcp import get_credentials

DATASET_ID = "kc_handson_scm"
LOCATION = "us-central1"
GLOSSARY_ID = "kc-handson-scm-glossary"
AWS_EG_ID = "aws-rds-scm"
API = "https://dataplex.googleapis.com/v1"
SYS = "projects/dataplex-types/locations/global"
GENERIC_ENTRY_TYPE = f"{SYS}/entryTypes/generic"


def resolve_project(explicit: str | None) -> str:
    """プロジェクト ID を解決する。
    優先順位: --project 引数 > HANDSON_PROJECT_ID > gcloud config > Cloud Shell 環境変数。
    ※ Cloud Shell は起動時に GOOGLE_CLOUD_PROJECT / DEVSHELL_PROJECT_ID を自動注入し、その後
      `gcloud config set project` を実行しても更新されないため、gcloud config を環境変数より優先する。
    """
    if explicit:
        return explicit
    val = os.environ.get("HANDSON_PROJECT_ID", "").strip()
    if val:
        return val
    try:
        out = subprocess.run(
            ["gcloud", "config", "get-value", "project"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        out = ""
    if out and out != "(unset)":
        return out
    for env_key in ("GOOGLE_CLOUD_PROJECT", "DEVSHELL_PROJECT_ID", "GCLOUD_PROJECT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    raise RuntimeError("Google Cloud プロジェクト ID が未設定です。'gcloud config set project <PROJECT_ID>' を実行してください。")


# 現役 10 品目定義:
# (MATNR, MAKTX, CAT_CD, UMREZ, ZZOLD_MAT, V0_pcs, V1_confirmed_pcs, V9_landing_pcs, ZL01_cs, ZL02_transfer_cs, ZSMP_sample_cs)
# ※ 正解（V1 確定計画 vs ZL01 実売出荷個数 [M003はM903の350CS=8400個を合算]）:
#   1位 M007: V1=10,000 | 実売 1,250 CS * 12 = 15,000個 -> +50.0%
#   2位 M002: V1=24,000 | 実売 1,300 CS * 12 = 15,600個 -> -35.0%  (罠: ZL02横持ちが600CSあり除外しないと-5.0%に化ける)
#   3位 M009: V1=18,000 | 実売   525 CS * 24 = 12,600個 -> -30.0%
#   4位 M005: V1=24,000 | 実売   625 CS * 48 = 30,000個 -> +25.0%
#   5位 M001: V1=15,000 | 実売 1,000 CS * 12 = 12,000個 -> -20.0%
#   --- 以下は乖離 ±4% 以内の正常品目（ただしルールを知らないと上位に誤検出されるトラップ内蔵） ---
#   M003: V1=12,000 | 新品番150CS(3,600個) + 旧M903 350CS(8,400個) = 12,000個 -> 0.0%! (合算しないと -70.0% で誤検出!)
#   M004: V1=12,000 | 実売 520 CS(12,480個, +4.0%) だが ZL02横持ち400CS(9,600個) があり除外しないと +84.0% で1位誤検出!
#   M006: V1=10,000 | 実売 800 CS( 9,600個, -4.0%) だが ZSMPサンプル350CS(4,200個) があり除外しないと +38.0% で誤検出!
#   M008: V1= 9,600 | 実売 410 CS( 9,840個, +2.5%)
#   M010: V1=12,000 | 実売 980 CS(11,760個, -2.0%)
ACTIVE_ITEMS = [
    ("M001", "フレッシュハーブ柔軟剤 600ml", "LAUNDRY", 12, None, 16000, 15000, 12100, 1000, 80, 20),
    ("M002", "酵素パワー洗濯洗剤 900g", "LAUNDRY", 12, None, 25000, 24000, 15800, 1300, 600, 30),
    ("M003", "クリアミント歯みがき 130g", "ORAL", 24, "M903", 13000, 12000, 11900, 150, 40, 10),
    ("M004", "知覚過敏ケア歯みがき 90g", "ORAL", 24, None, 12500, 12000, 12400, 520, 400, 15),
    ("M005", "ホワイトニング歯ブラシ ふつう", "ORAL", 48, None, 23000, 24000, 29800, 625, 30, 10),
    ("M006", "デンタルリンス ノンアルコール 500ml", "ORAL", 12, None, 10500, 10000, 9700, 800, 50, 350),
    ("M007", "泡ハンドソープ 詰替大型 800ml", "HAND", 12, None, 9500, 10000, 14900, 1250, 100, 25),
    ("M008", "薬用ハンドジェル 250ml", "HAND", 24, None, 9600, 9600, 9800, 410, 30, 10),
    ("M009", "速乾食器用洗剤 シトラス 240ml", "KITCHEN", 24, None, 19000, 18000, 12700, 525, 50, 15),
    ("M010", "食洗機用ジェル洗剤 480g", "KITCHEN", 12, None, 12200, 12000, 11800, 980, 60, 20),
]

LEGACY_ITEM = ("M903", "クリアミント歯みがき 130g (旧品)", "ORAL", 24, None, 350)  # 9月の旧品実売出荷 350 CS (=8,400個)


def split_into_parts(total: int, n: int) -> list[int]:
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def create_bigquery_tables(project: str) -> None:
    bq = bigquery.Client(project=project, location=LOCATION, credentials=get_credentials(project))
    ds_ref = bigquery.Dataset(f"{project}.{DATASET_ID}")
    ds_ref.location = LOCATION
    bq.create_dataset(ds_ref, exists_ok=True)
    print(f"  [BigQuery] データセット確認/作成完了: {project}.{DATASET_ID} ({LOCATION})")

    # 1. ZMATMST (品目マスタ: 現役10 + 旧品1 = 11行、列 description 空)
    mat_rows = [
        {"MATNR": m[0], "MAKTX": m[1], "CAT_CD": m[2], "UMREZ": m[3], "ZZOLD_MAT": m[4]} for m in ACTIVE_ITEMS
    ]
    mat_rows.append(
        {
            "MATNR": LEGACY_ITEM[0],
            "MAKTX": LEGACY_ITEM[1],
            "CAT_CD": LEGACY_ITEM[2],
            "UMREZ": LEGACY_ITEM[3],
            "ZZOLD_MAT": LEGACY_ITEM[4],
        }
    )
    mat_schema = [
        bigquery.SchemaField("MATNR", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("MAKTX", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("CAT_CD", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("UMREZ", "INT64", mode="REQUIRED"),
        bigquery.SchemaField("ZZOLD_MAT", "STRING", mode="NULLABLE"),
    ]
    bq.load_table_from_json(
        mat_rows,
        f"{project}.{DATASET_ID}.ZMATMST",
        job_config=bigquery.LoadJobConfig(schema=mat_schema, write_disposition="WRITE_TRUNCATE"),
    ).result()
    print(f"  [BigQuery] テーブル作成完了: ZMATMST ({len(mat_rows)} 行)")

    # 2. ZPLAN_BUILDUP (販売計画: 2026-08, 2026-09, 2026-10 × V0/V1/V9 = 90行)
    plan_rows = []
    for matnr, _, _, _, _, v0, v1, v9, _, _, _ in ACTIVE_ITEMS:
        for ver, qty in (("V0", int(v1 * 0.96)), ("V1", v1), ("V9", int(v1 * 0.99))):
            plan_rows.append({"SPMON": "2026-08", "MATNR": matnr, "VERSN": ver, "PLNMG": qty})
        for ver, qty in (("V0", v0), ("V1", v1), ("V9", v9)):
            plan_rows.append({"SPMON": "2026-09", "MATNR": matnr, "VERSN": ver, "PLNMG": qty})
        for ver, qty in (("V0", int(v1 * 1.05)), ("V1", int(v1 * 1.02)), ("V9", int(v1 * 1.02))):
            plan_rows.append({"SPMON": "2026-10", "MATNR": matnr, "VERSN": ver, "PLNMG": qty})

    plan_schema = [
        bigquery.SchemaField("SPMON", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("MATNR", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("VERSN", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("PLNMG", "INT64", mode="REQUIRED"),
    ]
    bq.load_table_from_json(
        plan_rows,
        f"{project}.{DATASET_ID}.ZPLAN_BUILDUP",
        job_config=bigquery.LoadJobConfig(schema=plan_schema, write_disposition="WRITE_TRUNCATE"),
    ).result()
    print(f"  [BigQuery] テーブル作成完了: ZPLAN_BUILDUP ({len(plan_rows)} 行)")

    # 3. ZSHIP (出荷実績: LFART = ZL01 / ZL02 / ZSMP、数量列 = LFIMG [ケース単位])
    ship_days_sep = [dt.date(2026, 9, d) for d in (3, 7, 11, 15, 19, 23, 26, 29)]
    ship_days_aug = [dt.date(2026, 8, d) for d in (5, 14, 24)]
    ship_rows = []

    def add_ship_records(matnr: str, total_cs: int, lfart: str, days: list[dt.date]) -> None:
        parts = split_into_parts(total_cs, len(days) * 2)
        idx = 0
        for d in days:
            for werks in ("W01", "W02"):
                if parts[idx] > 0:
                    ship_rows.append(
                        {"WADAT": d.isoformat(), "MATNR": matnr, "WERKS": werks, "LFART": lfart, "LFIMG": parts[idx]}
                    )
                idx += 1

    for matnr, _, _, _, _, _, _, _, zl01_cs, zl02_cs, zsmp_cs in ACTIVE_ITEMS:
        add_ship_records(matnr, zl01_cs, "ZL01", ship_days_sep)
        add_ship_records(matnr, zl02_cs, "ZL02", ship_days_sep[:4])
        add_ship_records(matnr, zsmp_cs, "ZSMP", ship_days_sep[:2])
        add_ship_records(matnr, int(zl01_cs * 0.95), "ZL01", ship_days_aug)

    # 旧品番 M903 の 9 月初旬在庫消化出荷 (ZL01: 350 CS)
    add_ship_records(LEGACY_ITEM[0], LEGACY_ITEM[5], "ZL01", ship_days_sep[:4])

    ship_schema = [
        bigquery.SchemaField("WADAT", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("MATNR", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("WERKS", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("LFART", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("LFIMG", "INT64", mode="REQUIRED"),
    ]
    bq.load_table_from_json(
        ship_rows,
        f"{project}.{DATASET_ID}.ZSHIP",
        job_config=bigquery.LoadJobConfig(schema=ship_schema, write_disposition="WRITE_TRUNCATE"),
    ).result()
    print(f"  [BigQuery] テーブル作成完了: ZSHIP ({len(ship_rows)} 行)")


def _kc_req(sess: AuthorizedSession, method: str, path: str, ok404: bool = False, **kw) -> dict | None:
    url = path if path.startswith("http") else f"{API}/{path}"
    r = sess.request(method, url, **kw)
    if r.status_code == 404 and ok404:
        return None
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:600]}")
    return r.json() if r.text else {}


def _wait_op(sess: AuthorizedSession, op: dict) -> None:
    name = op.get("name")
    if not name or op.get("done"):
        return
    for _ in range(45):
        time.sleep(2)
        cur = _kc_req(sess, "GET", name) or {}
        if cur.get("done"):
            if "error" in cur:
                raise RuntimeError(json.dumps(cur["error"], ensure_ascii=False))
            return
    raise TimeoutError(f"Operation timeout: {name}")


def seed_initial_catalog(project: str) -> None:
    """横断検索と Enrichment の Knowledge signal として使う初期シードを登録する。
    ※ BigQuery テーブルエントリ自体は一切変更しない（Step 2 の Before 体験を守るため）。
    """
    sess = AuthorizedSession(get_credentials(project))
    parent = f"projects/{project}/locations/{LOCATION}"

    g_name = f"{parent}/glossaries/{GLOSSARY_ID}"
    if not _kc_req(sess, "GET", g_name, ok404=True):
        op = _kc_req(
            sess,
            "POST",
            f"{parent}/glossaries",
            params={"glossaryId": GLOSSARY_ID},
            json={
                "displayName": "日用品SCM 需給計画用語集 (Hands-on)",
                "description": "販売計画・出荷実績・予実乖離に関する標準業務用語集",
            },
        )
        _wait_op(sess, op or {})
        print(f"  [Knowledge Catalog] Business Glossary 作成完了: {GLOSSARY_ID}")
    else:
        print(f"  [Knowledge Catalog] Business Glossary 確認済み: {GLOSSARY_ID}")

    seed_terms = [
        (
            "confirmed-sales-plan",
            "確定販売計画 (VERSN=V1)",
            "販売計画テーブル ZPLAN_BUILDUP の VERSN='V1'（月初確定計画）を指す。'V0'（素案）や 'V9'（月末着地見込み）は予実評価の基準に使ってはならない。",
        ),
        (
            "actual-customer-shipment",
            "実売出荷個数 (LFART=ZL01・新旧合算)",
            "出荷実績 ZSHIP のうち伝票タイプ LFART='ZL01'（得意先販売出荷）のみを対象とし（'ZL02'倉庫間転送・'ZSMP'無償サンプルは除外）、"
            "ケース数量 LFIMG に品目マスタ ZMATMST.UMREZ を掛けて個数換算した値。リニューアル品目（ZZOLD_MAT 設定あり）は旧品番の実売出荷個数を新品番へ合算する。",
        ),
    ]
    for tid, tname, tdesc in seed_terms:
        t_path = f"{g_name}/terms/{tid}"
        body = {"displayName": tname, "description": tdesc, "parent": g_name}
        if _kc_req(sess, "GET", t_path, ok404=True):
            _kc_req(sess, "PATCH", t_path, params={"updateMask": "displayName,description"}, json=body)
        else:
            _kc_req(sess, "POST", f"{g_name}/terms", params={"termId": tid}, json=body)
    print("  [Knowledge Catalog] Business Glossary 初期用語 (2語) 登録完了")

    eg_name = f"{parent}/entryGroups/{AWS_EG_ID}"
    if not _kc_req(sess, "GET", eg_name, ok404=True):
        op = _kc_req(
            sess,
            "POST",
            f"{parent}/entryGroups",
            params={"entryGroupId": AWS_EG_ID},
            json={
                "displayName": "AWS RDS (MySQL) 連携メタデータ",
                "description": "AWS 環境の基幹周辺 DB（販促・特売管理 MySQL）のテーブル定義を管理するエントリグループ",
            },
        )
        _wait_op(sess, op or {})

    entry_id = "mysql_promo_schedule"
    entry_name = f"{eg_name}/entries/{entry_id}"
    ov_md = (
        "# AWS RDS (MySQL): promo_db.promo_schedule（特売・チラシ施策マスタ）\n\n"
        "- **ホスト環境**: AWS ap-northeast-1 / Amazon RDS for MySQL\n"
        "- **業務概要**: 量販店チェーン別の特売・チラシ掲載スケジュールを管理するテーブル。品目コード `item_cd` は BigQuery `ZMATMST.MATNR` と対応。\n"
        "- **データ移動なしでの統合**: 実データは AWS 側に置いたまま、Knowledge Catalog でスキーマと意味定義を一元検索可能にしているサンプル。"
    )
    entry_body = {
        "entryType": GENERIC_ENTRY_TYPE,
        "entrySource": {
            "displayName": "promo_db.promo_schedule (AWS RDS MySQL)",
            "description": "AWS RDS 上の特売・チラシ販促スケジュールテーブル（MATNR で BigQuery 出荷実績と紐付け可能）",
            "system": "Amazon-RDS-MySQL",
        },
        "aspects": {
            "dataplex-types.global.generic": {"data": {"type": "TABLE", "system": "Amazon-RDS-MySQL"}},
            "dataplex-types.global.overview": {"data": {"content": ov_md, "contentType": "MARKDOWN"}},
        },
    }
    if _kc_req(sess, "GET", entry_name, ok404=True):
        _kc_req(
            sess,
            "PATCH",
            entry_name,
            params=[
                ("updateMask", "aspects,entrySource"),
                ("aspectKeys", "dataplex-types.global.generic"),
                ("aspectKeys", "dataplex-types.global.overview"),
            ],
            json=entry_body,
        )
    else:
        _kc_req(sess, "POST", f"{eg_name}/entries", params={"entryId": entry_id}, json=entry_body)
    print(f"  [Knowledge Catalog] AWS RDS 想定カスタムエントリ登録完了: {entry_id}")


def verify_golden(project: str) -> None:
    bq = bigquery.Client(project=project, location=LOCATION, credentials=get_credentials(project))
    sql = f"""
    WITH confirmed_plan AS (
      SELECT p.MATNR, m.MAKTX, m.ZZOLD_MAT, p.PLNMG AS plan_pcs
      FROM `{project}.{DATASET_ID}.ZPLAN_BUILDUP` p
      JOIN `{project}.{DATASET_ID}.ZMATMST` m ON p.MATNR = m.MATNR
      WHERE p.SPMON = '2026-09' AND p.VERSN = 'V1'
    ),
    ship_by_mat AS (
      SELECT s.MATNR, SUM(s.LFIMG * m.UMREZ) AS ship_pcs
      FROM `{project}.{DATASET_ID}.ZSHIP` s
      JOIN `{project}.{DATASET_ID}.ZMATMST` m ON s.MATNR = m.MATNR
      WHERE FORMAT_DATE('%Y-%m', s.WADAT) = '2026-09' AND s.LFART = 'ZL01'
      GROUP BY s.MATNR
    )
    SELECT
      cp.MATNR,
      cp.MAKTX,
      cp.plan_pcs,
      COALESCE(sn.ship_pcs, 0) + COALESCE(so.ship_pcs, 0) AS actual_pcs,
      ROUND(SAFE_DIVIDE((COALESCE(sn.ship_pcs, 0) + COALESCE(so.ship_pcs, 0)) - cp.plan_pcs, cp.plan_pcs) * 100, 1) AS deviation_pct
    FROM confirmed_plan cp
    LEFT JOIN ship_by_mat sn ON cp.MATNR = sn.MATNR
    LEFT JOIN ship_by_mat so ON cp.ZZOLD_MAT = so.MATNR
    ORDER BY ABS(deviation_pct) DESC
    LIMIT 5
    """
    print("\n=== [検証] Golden SQL 実行結果（2026年9月 予実乖離率 ワースト5）===")
    for row in bq.query(sql).result():
        print(f"  {row.MATNR} | {row.MAKTX:<24} | 計画: {row.plan_pcs:>6,}個 | 実績: {row.actual_pcs:>6,}個 | 乖離率: {row.deviation_pct:>+6.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--seed-catalog", action="store_true")
    ap.add_argument("--verify-golden", action="store_true")
    args = ap.parse_args()

    project = resolve_project(args.project)
    create_bigquery_tables(project)
    if args.seed_catalog:
        seed_initial_catalog(project)
    if args.verify_golden:
        verify_golden(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
