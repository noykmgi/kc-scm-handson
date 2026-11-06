"""Knowledge Catalog & Knowledge Studio ハンズオン用:
コンテキスト一括反映（Fast-Forward 救済用）＆ カタログリソース削除スクリプト。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from google.auth.transport.requests import AuthorizedSession

from inspect_mcp import get_credentials

DATASET_ID = "kc_handson_scm"
LOCATION = "us-central1"
GLOSSARY_ID = "kc-handson-scm-glossary"
CONTEXT_SET_ID = "kc-handson-scm-context"
AWS_EG_ID = "aws-rds-scm"
ASPECT_TYPE_ID = "kc-handson-business-rule"

API = "https://dataplex.googleapis.com/v1"
SYS = "projects/dataplex-types/locations/global"
GENERIC_ENTRY_TYPE = f"{SYS}/entryTypes/generic"
OV_KEY = "dataplex-types.global.overview"
GEN_KEY = "dataplex-types.global.generic"


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


class KCClient:
    def __init__(self, project: str, location: str = LOCATION):
        self.s = AuthorizedSession(get_credentials(project))
        self.project = project
        self.location = location
        self.parent = f"projects/{project}/locations/{location}"

    def req(self, method: str, path: str, ok404: bool = False, **kw) -> dict | None:
        url = path if path.startswith("http") else f"{API}/{path}"
        r = self.s.request(method, url, **kw)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.text else {}

    def wait_op(self, op: dict | None) -> None:
        if not op:
            return
        name = op.get("name")
        if not name or op.get("done"):
            return
        for _ in range(45):
            time.sleep(2)
            cur = self.req("GET", name) or {}
            if cur.get("done"):
                if "error" in cur:
                    raise RuntimeError(json.dumps(cur["error"], ensure_ascii=False))
                return
        raise TimeoutError(f"Operation timeout: {name}")


TABLE_OVERVIEWS = {
    "ZMATMST": """# 品目マスタ (ZMATMST)

日用品の品目マスタテーブル（現役品目およびリニューアル前の旧品番を含む）。

## カラム定義
- `MATNR` (STRING, PK): 品目コード（例: `M001` 〜 `M010`、旧品番 `M903`）
- `MAKTX` (STRING): 品目名
- `CAT_CD` (STRING): カテゴリコード（`LAUNDRY`, `ORAL`, `HAND`, `KITCHEN`）
- `UMREZ` (INT64): **1ケースあたりのバラ入数**（品目ごとに `12`, `24`, `48` と異なる）
- `ZZOLD_MAT` (STRING, NULLABLE): **リニューアル前の旧品番コード**（例: `M003` の `ZZOLD_MAT` は `'M903'`）

## 業務ルール（必須）
- **単位換算ルール**: 出荷実績 `ZSHIP.LFIMG` はケース単位であるため、必ず品目マスタを結合し `ZSHIP.LFIMG * ZMATMST.UMREZ` でバラ個数に換算すること。
- **リニューアル新旧合算ルール**: `ZZOLD_MAT` に旧品番が設定されている品目は、旧品番の実売出荷個数（`LFART = 'ZL01'`）も新品番に合算して予実比較すること（販売計画は新品番側にのみ登録されている）。
""",
    "ZPLAN_BUILDUP": """# 販売計画テーブル (ZPLAN_BUILDUP)

月次・品目別の販売計画テーブル。

## カラム定義
- `SPMON` (STRING): 計画対象年月（`YYYY-MM` 形式、例: `'2026-09'`）
- `MATNR` (STRING): 品目コード（`ZMATMST.MATNR` と結合）
- `VERSN` (STRING): **計画バージョン区分コード**
  - `'V0'`: 営業積上げ素案（未承認）
  - **`'V1'`: 月初確定販売計画（公式の予実評価基準！）**
  - `'V9'`: 月末着地見込み（ローリングフォーキャスト。計画値ではないため予実比較への使用厳禁！）
- `PLNMG` (INT64): **計画数量（単位: バラ個数 / Pieces）**

## 業務ルール（必須）
- **確定計画バージョンルール**: 予実乖離の評価や確定計画の集計では、**必ず `VERSN = 'V1'` で絞り込むこと**。`'V9'`（月末着地見込み）を使うことや複数バージョンの合算は厳禁。
""",
    "ZSHIP": """# 出荷実績テーブル (ZSHIP)

物流倉庫からの日次出荷トランザクション（得意先出荷・倉庫間転送・サンプル出荷が混在）。

## カラム定義
- `WADAT` (DATE): 出荷計上日（月次集計時は `FORMAT_DATE('%Y-%m', WADAT)` で `SPMON` と合わせる）
- `MATNR` (STRING): 出荷された品目コード（リニューアル切替月は旧品番での出荷明細も含まれる）
- `WERKS` (STRING): 出荷倉庫コード（`W01`=東日本, `W02`=西日本）
- `LFART` (STRING): **出荷伝票タイプ（社内アドオン区分）**
  - **`'ZL01'`: 得意先向け実売出荷（販売計画と比較する唯一の集計対象！）**
  - `'ZL02'`: 倉庫間の在庫横持ち転送（実売ではないため必ず除外すること！）
  - `'ZSMP'`: 販促・展示用サンプル無償出荷（必ず除外すること！）
- `LFIMG` (INT64): **出荷数量（単位: ケース数 / Cases）**

## 業務ルール（必須）
1. **伝票タイプ絞り込み**: 実売出荷を集計する際は **必ず `LFART = 'ZL01'` のみにフィルタすること**（`'ZL02'` と `'ZSMP'` は除外）。
2. **ケース→バラ個数換算**: `LFIMG` はケース単位のため、`SUM(ZSHIP.LFIMG * ZMATMST.UMREZ)` でバラ個数に換算してから計画 `PLNMG`（個数）と比較すること。
3. **リニューアル合算**: `ZMATMST.ZZOLD_MAT` を持つ品目は、旧品番の `ZL01` 出荷個数を新品番に合算すること。
4. **予実乖離率**: `(実売出荷個数 - 確定計画個数[V1]) / 確定計画個数[V1]`。乖離上位品目は絶対値 `ABS(...)` の降順で抽出すること。
""",
}

GLOSSARY_TERMS = [
    (
        "confirmed-sales-plan",
        "確定販売計画 (VERSN=V1)",
        "販売計画 ZPLAN_BUILDUP の VERSN='V1'（月初確定計画）。'V0'（素案）や 'V9'（月末着地見込み）は予実評価の基準に使ってはならない。",
    ),
    (
        "actual-customer-shipment",
        "実売出荷個数 (LFART=ZL01・新旧合算)",
        "ZSHIP のうち LFART='ZL01'（得意先販売出荷）のみを対象とし（'ZL02'倉庫間横持ち・'ZSMP'無償サンプルは除外）、"
        "ケース数量 LFIMG × ZMATMST.UMREZ で個数換算した値。ZZOLD_MAT があるリニューアル品目は旧品番の ZL01 出荷個数を新品番へ合算する。",
    ),
    (
        "plan-vs-actual-deviation-rate",
        "予実乖離率",
        "計算式: (実売出荷個数 - 確定販売計画個数[VERSN='V1']) / 確定販売計画個数[VERSN='V1']。"
        "乖離が大きい上位品目は絶対値 ABS() の降順でランキングし、±20% 以上を要注意品目とする。",
    ),
]


def apply_fast_forward(kc: KCClient) -> None:
    print(f"=== Knowledge Catalog コンテキスト一括反映 ({kc.project} / {kc.location}) ===")

    global_parent = f"projects/{kc.project}/locations/global"
    at_name = f"{global_parent}/aspectTypes/{ASPECT_TYPE_ID}"
    if not kc.req("GET", at_name, ok404=True):
        op = kc.req(
            "POST",
            f"{global_parent}/aspectTypes",
            params={"aspectTypeId": ASPECT_TYPE_ID},
            json={
                "displayName": "SCM 業務集計ルール (Hands-on)",
                "metadataTemplate": {
                    "name": "ScmBusinessRule",
                    "type": "record",
                    "recordFields": [
                        {"index": 1, "name": "domain", "type": "string"},
                        {"index": 2, "name": "critical_rule", "type": "string"},
                    ],
                },
            },
        )
        kc.wait_op(op)
        print(f"  [1/4] カスタム AspectType 作成完了: {ASPECT_TYPE_ID}")
    else:
        print(f"  [1/4] カスタム AspectType 確認済み: {ASPECT_TYPE_ID}")

    g_name = f"{kc.parent}/glossaries/{GLOSSARY_ID}"
    if not kc.req("GET", g_name, ok404=True):
        op = kc.req(
            "POST",
            f"{kc.parent}/glossaries",
            params={"glossaryId": GLOSSARY_ID},
            json={
                "displayName": "日用品SCM 需給計画用語集 (Hands-on)",
                "description": "販売計画・出荷実績・予実乖離に関する標準業務用語集",
            },
        )
        kc.wait_op(op)
    for tid, tname, tdesc in GLOSSARY_TERMS:
        t_path = f"{g_name}/terms/{tid}"
        body = {"displayName": tname, "description": tdesc, "parent": g_name}
        if kc.req("GET", t_path, ok404=True):
            kc.req("PATCH", t_path, params={"updateMask": "displayName,description"}, json=body)
        else:
            kc.req("POST", f"{g_name}/terms", params={"termId": tid}, json=body)
    print(f"  [2/4] Business Glossary 用語 ({len(GLOSSARY_TERMS)}語) 登録完了")

    rule_key = f"{kc.project}.global.{ASPECT_TYPE_ID}"
    rule_texts = {
        "ZMATMST": "LFIMG * UMREZ で個数換算し、ZZOLD_MAT がある品目は旧品番の実売出荷を新品番に合算する",
        "ZPLAN_BUILDUP": "予実評価では必ず VERSN='V1'(月初確定計画) を採用し、'V9'(着地見込) は使用しない",
        "ZSHIP": "必ず LFART='ZL01'(実売出荷) のみに絞り、'ZL02'(横持ち)・'ZSMP'(サンプル) は除外する",
    }
    for tbl, md in TABLE_OVERVIEWS.items():
        entry_name = (
            f"{kc.parent}/entryGroups/@bigquery/entries/"
            f"bigquery.googleapis.com/projects/{kc.project}/datasets/{DATASET_ID}/tables/{tbl}"
        )
        for _ in range(6):
            if kc.req("GET", entry_name, ok404=True):
                break
            time.sleep(3)
        aspects = {
            OV_KEY: {"data": {"content": md.strip(), "contentType": "MARKDOWN"}},
            rule_key: {"data": {"domain": "SCM需給計画", "critical_rule": rule_texts[tbl]}},
        }
        kc.req(
            "PATCH",
            entry_name,
            params=[("updateMask", "aspects"), ("aspectKeys", OV_KEY), ("aspectKeys", rule_key)],
            json={"aspects": aspects},
        )
    print("  [3/4] BigQuery 3テーブルへの Overview & 業務ルール Aspect 付与完了")

    cs_name = f"{kc.parent}/entryGroups/{CONTEXT_SET_ID}"
    if not kc.req("GET", cs_name, ok404=True):
        op = kc.req(
            "POST",
            f"{kc.parent}/entryGroups",
            params={"entryGroupId": CONTEXT_SET_ID},
            json={
                "displayName": "SCM 需給計画コンテキストセット (Hands-on)",
                "description": "販売計画・出荷実績の予実乖離分析用コンテキストセット",
                "type": "context-set",
            },
        )
        kc.wait_op(op)

    guide_md = (
        "# SCM 予実乖離分析 4大必須ルール (SOP Summary)\n\n"
        "1. **確定計画バージョン (`ZPLAN_BUILDUP`)**: 必ず `VERSN = 'V1'`（月初確定計画）のみを採用すること（`'V9'` は月末着地見込みのため使用厳禁）。\n"
        "2. **実売出荷フィルタ (`ZSHIP`)**: 必ず `LFART = 'ZL01'`（得意先実売出荷）のみを集計すること（`'ZL02'` 倉庫間横持ち転送、`'ZSMP'` 無償サンプルは除外）。\n"
        "3. **ケース→バラ個数換算**: `ZSHIP.LFIMG` はケース単位。品目マスタ `ZMATMST.UMREZ` を掛けてバラ個数に換算すること。\n"
        "4. **リニューアル品目の新旧合算**: `ZMATMST.ZZOLD_MAT` に旧品番がある新品番（例: `M003`←`M903`）は、旧品番の `ZL01` 出荷個数を新品番へ合算して予実乖離率 `(実売出荷個数 - 確定計画個数) / 確定計画個数` を算出すること。"
    )
    doc_entry_id = "documents_scm_sop_summary"
    doc_entry_name = f"{cs_name}/entries/{doc_entry_id}"
    doc_body = {
        "entryType": GENERIC_ENTRY_TYPE,
        "entrySource": {
            "displayName": "SCM 予実乖離分析 標準業務ルール (SOP)",
            "description": "販売計画(VERSN=V1)・実売出荷(LFART=ZL01)・ケース換算(UMREZ)・新旧品番合算(ZZOLD_MAT)の公式定義",
            "system": "kc-handson-scm",
        },
        "aspects": {
            GEN_KEY: {"data": {"type": "DOCUMENT", "system": "kc-handson-scm"}},
            OV_KEY: {"data": {"content": guide_md, "contentType": "MARKDOWN"}},
        },
    }
    if kc.req("GET", doc_entry_name, ok404=True):
        kc.req(
            "PATCH",
            doc_entry_name,
            params=[("updateMask", "aspects,entrySource"), ("aspectKeys", GEN_KEY), ("aspectKeys", OV_KEY)],
            json=doc_body,
        )
    else:
        kc.req("POST", f"{cs_name}/entries", params={"entryId": doc_entry_id}, json=doc_body)
    print(f"  [4/4] Context Set エントリ登録完了: {CONTEXT_SET_ID}/{doc_entry_id}")
    print("\n✅ 完了: リモート MCP (`inspect_mcp.py`) および After エージェント (`ask_agent.py --mode kc`) の準備が整いました！")


def cleanup_catalog(kc: KCClient) -> None:
    print(f"=== Knowledge Catalog ハンズオンリソース削除 ({kc.project}) ===")

    for eg_id in (CONTEXT_SET_ID, AWS_EG_ID):
        eg_name = f"{kc.parent}/entryGroups/{eg_id}"
        if kc.req("GET", eg_name, ok404=True):
            res = kc.req("GET", f"{eg_name}/entries", params={"pageSize": 200}) or {}
            for e in res.get("entries", []):
                if e["name"].endswith(f"/entries/{eg_id}_entry"):
                    continue
                kc.req("DELETE", e["name"], ok404=True)
            kc.wait_op(kc.req("DELETE", eg_name, ok404=True))
            print(f"  削除完了: EntryGroup {eg_id}")

    g_name = f"{kc.parent}/glossaries/{GLOSSARY_ID}"
    if kc.req("GET", g_name, ok404=True):
        terms_res = kc.req("GET", f"{g_name}/terms", params={"pageSize": 200}) or {}
        for t in terms_res.get("terms", []):
            kc.req("DELETE", t["name"], ok404=True)
        cats_res = kc.req("GET", f"{g_name}/categories", params={"pageSize": 200}) or {}
        for c in cats_res.get("categories", []):
            kc.req("DELETE", c["name"], ok404=True)
        kc.wait_op(kc.req("DELETE", g_name, ok404=True))
        print(f"  削除完了: Glossary {GLOSSARY_ID}")

    at_name = f"projects/{kc.project}/locations/global/aspectTypes/{ASPECT_TYPE_ID}"
    if kc.req("GET", at_name, ok404=True):
        kc.wait_op(kc.req("DELETE", at_name, ok404=True))
        print(f"  削除完了: AspectType {ASPECT_TYPE_ID}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    project = resolve_project(args.project)
    kc = KCClient(project, LOCATION)
    if args.cleanup:
        cleanup_catalog(kc)
    else:
        apply_fast_forward(kc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
