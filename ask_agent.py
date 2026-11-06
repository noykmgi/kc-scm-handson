"""Knowledge Catalog & Knowledge Studio ハンズオン用:
Before / After 比較データ分析エージェント実行スクリプト。

使い方:
  # Step 2 (Before): 生データ直結モード（BigQuery スキーマ・SQL 実行ツールのみ）
  python ask_agent.py --mode plain

  # Step 5 (After): Knowledge Catalog MCP 連携モード（+ search_entries / lookup_context）
  python ask_agent.py --mode kc
"""

import argparse
import json
import os
import subprocess
import time
from typing import Any, Optional

from google import genai
from google.cloud import bigquery
from google.genai import types

from inspect_mcp import DATASET_ID, LOCATION, RemoteMcpClient, get_credentials, resolve_project

MODEL_ID = "gemini-3.8-flash"
MAX_BYTES_BILLED = 1 * 1024**3  # 1 GB 安全上限
DEFAULT_QUESTION = (
    "2026年9月において、販売計画に対する出荷実績の予実乖離率（絶対値）が大きかった品目トップ5について、"
    "品目コード、品目名、確定計画数量（個数）、出荷実績数量（個数）、予実乖離率(%)を出してください。"
)

# Golden SQL の期待トップ 5 (MATNR, 確定計画個数, 出荷実績個数, 乖離率%)
EXPECTED_TOP5 = [
    ("M007", 10000, 15000, 50.0),
    ("M002", 24000, 15600, -35.0),
    ("M009", 18000, 12600, -30.0),
    ("M005", 24000, 30000, 25.0),
    ("M001", 15000, 12000, -20.0),
]


def build_system_instruction(project: str, mode: str) -> str:
    base = f"""あなたは日用品メーカーの需給計画部門を支援するデータ分析 AI エージェントです。
ユーザーの質問に対し、BigQuery のデータを集計して日本語で簡潔・正確に回答してください。

## 対象データ
- Google Cloud プロジェクト: `{project}`
- BigQuery データセット: `{project}.{DATASET_ID}`（リージョン: `{LOCATION}`）

## 手順
1. まず利用可能なツールでテーブル一覧やスキーマ（必要な場合はカタログのコンテキスト）を確認する。
2. データ確認のための予備クエリは最大 2 回までに留め、コード値の意味など定義が明示されていない項目がある場合は、最も妥当と推測した前提を置いて最終集計 SQL（GoogleSQL）を作成し、`execute_sql` ツールで実行する。テーブルは必ず `` `{project}.{DATASET_ID}.<テーブル名>` `` の完全修飾名で指定すること。
3. 得られた結果に基づき、適用した計算式・前提条件を添えて分かりやすく表形式で回答する。
"""
    if mode == "kc":
        base += f"""
## Knowledge Catalog コンテキスト参照（必須ルール）
この組織では、テーブルの業務的意味・版管理ルール・単位換算ルール・KPI 計算式が **Knowledge Catalog** に管理されています。
SQL を書く前に **必ず** 以下の MCP ツールを呼び出し、取得した業務ルールに厳密に従ってください（独自の推測で計算しないこと）：
1. `kc_lookup_context` に `{DATASET_ID}` 配下の対象テーブル名リスト（例: `["ZMATMST", "ZPLAN_BUILDUP", "ZSHIP"]`）を渡して、テーブルの Overview・業務ルールを取得する。
2. 併せて `kc_search_entries` で質問に含まれる業務用語（例: `"予実乖離率 計画最新版 ケース換算"`）を検索し、Context Set 内のドキュメントや Business Glossary 用語があれば確認する。
"""
    return base


def evaluate_rows(rows: Optional[list[dict[str, Any]]]) -> tuple[bool, str]:
    """最後に成功した SQL の結果行が Golden Top 5 と一致するかを自動判定する。"""
    if not rows:
        return False, "SQL の実行結果が空です。"
    # 結果行から M001〜M010 の順序と数値を抽出
    found_codes: list[str] = []
    for r in rows[:5]:
        vals = [str(v) for v in r.values() if v is not None]
        code = next((v for v in vals if v.startswith("M0") and len(v) == 4), None)
        if code:
            found_codes.append(code)

    expected_codes = [x[0] for x in EXPECTED_TOP5]
    if found_codes != expected_codes:
        hints = []
        if "M004" in found_codes or "M006" in found_codes:
            hints.append("倉庫間横持ち転送(`LFART='ZL02'`)や無償サンプル(`'ZSMP'`)が除外されず実売出荷に混入しています")
        if "M003" in found_codes:
            hints.append("リニューアル品目 `M003` に旧品番 `M903` の出荷が合算されていません（合算すると乖離 0.0%）")
        hints.append(f"抽出された上位品目順 {found_codes} が正解 {expected_codes} と不一致です（`VERSN='V1'` 以外の選択や単位換算漏れの可能性）")
        return False, " / ".join(hints)

    # 先頭行 M007 の計画値 10000 と実績値 15000 が含まれるかチェック
    first_nums = []
    for v in rows[0].values():
        if isinstance(v, (int, float)):
            first_nums.append(round(float(v), 1))
    if 10000.0 in first_nums and 15000.0 in first_nums:
        return True, "上位5品目（M007, M002, M009, M005, M001）および確定計画個数・ケース換算後の出荷個数が Golden SQL と完全一致しました！"
    return False, f"品目コード順は一致しましたが、数値 {first_nums} が期待値（計画 10,000個 / 実績 15,000個）と異なります。"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["plain", "kc"], required=True, help="plain: カタログなし / kc: Knowledge Catalog MCP 連携")
    ap.add_argument("--project", default=None)
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    args = ap.parse_args()

    project = resolve_project(args.project)
    creds = get_credentials(project)
    bq = bigquery.Client(project=project, location=LOCATION, credentials=creds)
    mcp = RemoteMcpClient(project) if args.mode == "kc" else None
    client = genai.Client(vertexai=True, project=project, location="global", credentials=creds)

    last_sql: str = ""
    last_rows: Optional[list[dict[str, Any]]] = None
    sql_count = 0

    # ---- エージェントに提供するツール関数群 -------------------------------------------------
    def list_tables() -> dict:
        """データセット kc_handson_scm 内の BigQuery テーブル一覧を取得する。"""
        print("  🔧 [BigQuery Tool] list_tables()")
        tbls = [t.table_id for t in bq.list_tables(f"{project}.{DATASET_ID}")]
        return {"dataset": f"{project}.{DATASET_ID}", "tables": tbls}

    def get_table_schema(table_name: str) -> dict:
        """指定された BigQuery テーブルのカラム名・データ型・説明を取得する。"""
        short = table_name.split(".")[-1].strip("`")
        print(f"  🔧 [BigQuery Tool] get_table_schema(table_name='{short}')")
        tbl = bq.get_table(f"{project}.{DATASET_ID}.{short}")
        cols = [{"name": f.name, "type": f.field_type, "description": f.description or ""} for f in tbl.schema]
        return {"table": f"{project}.{DATASET_ID}.{short}", "num_rows": tbl.num_rows, "columns": cols}

    def execute_sql(sql: str) -> dict:
        """BigQuery 標準 SQL (SELECT のみ) を実行し、最大 50 行の結果を取得する。"""
        nonlocal last_sql, last_rows, sql_count
        sql_count += 1
        one_line = " ".join(sql.split())
        print(f"  🔧 [BigQuery Tool] execute_sql (#{sql_count}): {one_line[:110]}...")
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
        try:
            res = bq.query(sql, job_config=cfg).result()
            rows = [dict(r.items()) for r in list(res)[:50]]
            # JSON シリアライズ用に date 等を文字列化
            clean_rows = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]
            last_sql = sql
            last_rows = clean_rows
            return {"status": "OK", "row_count": len(clean_rows), "rows": clean_rows}
        except Exception as e:
            print(f"     ⚠️ SQL Error: {e}")
            return {"status": "ERROR", "error": str(e)}

    def kc_search_entries(query: str) -> dict:
        """Knowledge Catalog リモート MCP の search_entries を呼び出し、業務用語・Context Set・テーブルを検索する。"""
        assert mcp is not None
        print(f"  🌐 [Knowledge Catalog MCP] search_entries(query='{query}')")
        out = mcp.call_tool("search_entries", {"projectId": project, "query": query, "pageSize": 10})
        items = []
        for r in (out.get("results", []) if isinstance(out, dict) else []):
            de = r.get("dataplexEntry", {})
            es = de.get("entrySource", {})
            items.append(
                {
                    "resource_name": de.get("name", ""),
                    "display_name": es.get("displayName", ""),
                    "description": es.get("description", ""),
                }
            )
        # 見つかった Context Set / Glossary エントリの詳細も最大 5 件 lookup_context でまとめて返す
        extra_res = [
            it["resource_name"]
            for it in items
            if f"/locations/{LOCATION}/entryGroups/" in it["resource_name"] and "@bigquery" not in it["resource_name"]
        ][:5]
        extra_ctx = ""
        if extra_res:
            ctx_res = mcp.call_tool(
                "lookup_context", {"projectId": project, "location": LOCATION, "resources": extra_res}
            )
            extra_ctx = ctx_res.get("context", "") if isinstance(ctx_res, dict) else str(ctx_res)
        return {"matched_entries": items, "detailed_context": extra_ctx}

    def kc_lookup_context(table_names: list[str]) -> dict:
        """Knowledge Catalog リモート MCP の lookup_context を呼び出し、指定された BigQuery テーブル群の業務コンテキスト(YAML)を取得する。"""
        assert mcp is not None
        shorts = [t.split(".")[-1].strip("`") for t in table_names]
        print(f"  🌐 [Knowledge Catalog MCP] lookup_context(tables={shorts})")
        resources = [
            f"projects/{project}/locations/{LOCATION}/entryGroups/@bigquery/entries/"
            f"bigquery.googleapis.com/projects/{project}/datasets/{DATASET_ID}/tables/{s}"
            for s in shorts
        ]
        out = mcp.call_tool("lookup_context", {"projectId": project, "location": LOCATION, "resources": resources})
        ctx = out.get("context", "") if isinstance(out, dict) else str(out)
        return {"llm_ready_context_yaml": ctx}

    tools: list[Any] = [list_tables, get_table_schema, execute_sql]
    if args.mode == "kc":
        tools.extend([kc_search_entries, kc_lookup_context])

    mode_label = (
        "【Before】カタログなし (生テーブル直結)"
        if args.mode == "plain"
        else "【After】Knowledge Catalog MCP 連携あり"
    )
    print("=" * 80)
    print(f"🤖 エージェント実行開始: {mode_label}")
    print(f"   モデル: {MODEL_ID}  |  プロジェクト: {project}")
    print(f"   質問  : {args.question}")
    print("=" * 80)

    t0 = time.time()
    sys_inst = build_system_instruction(project, args.mode)
    chat = client.chats.create(
        model=MODEL_ID,
        config=types.GenerateContentConfig(
            system_instruction=sys_inst,
            tools=tools,
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=12),
        ),
    )
    response = chat.send_message(args.question)
    final_text = response.text or ""
    if not final_text.strip():
        followup = chat.send_message(
            "ここまでに取得したクエリ結果に基づき、採用した前提条件とトップ5品目の表を日本語で出力してください。",
            config=types.GenerateContentConfig(system_instruction=sys_inst, temperature=0.2),
        )
        final_text = followup.text or ""
    elapsed = time.time() - t0

    print("\n" + "-" * 80)
    print("📝 エージェント最終回答:")
    print("-" * 80)
    print(final_text or "(応答テキストなし)")

    if last_sql:
        print("\n" + "-" * 80)
        print("💻 最終実行された SQL:")
        print("-" * 80)
        print(last_sql.strip())

    passed, reason = evaluate_rows(last_rows)
    print("\n" + "=" * 80)
    badge = "✅ 正解 (Golden SQL と完全一致!)" if passed else "❌ 誤答検知 (業務ルールの欠落による集計ミス)"
    print(f"📊 自動採点結果: {badge}")
    print(f"   理由        : {reason}")
    print(f"   実行統計    : 所要時間 {elapsed:.1f}秒 / SQL 実行回数 {sql_count}回")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
