"""Knowledge Catalog & Knowledge Studio ハンズオン用:
フルマネージド リモート MCP サーバー (`https://dataplex.googleapis.com/mcp`) 動作確認スクリプト。

以下を順番に実行し、AI エージェントが裏側で受け取る「生の LLM 最適化コンテキスト（YAML）」を可視化します:
  1. `tools/list`     : リモート MCP サーバーが公開しているツール一覧の取得
  2. `search_entries` : 日本語の業務キーワード（「予実乖離」「AWS」等）による横断検索
  3. `lookup_context` : BigQuery テーブルと Context Set エントリから合成された YAML コンテキストの取得
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
from typing import Any

import google.auth
import google.auth.credentials
import google.auth.transport.requests
import requests

DATASET_ID = "kc_handson_scm"
LOCATION = "us-central1"
KC_MCP_URL = "https://dataplex.googleapis.com/mcp"


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


class GcloudCredentials(google.auth.credentials.CredentialsWithQuotaProject):
    """gcloud CLI のアクティブアカウント（`gcloud config get-value account`）の認証情報を使う Credentials。

    Cloud Shell では ADC（アプリケーションのデフォルト認証情報）と gcloud のアカウントは一致するが、
    ローカル端末や Cloudtop では別アカウントの ADC が残っていることがあり、その場合
    `gcloud config set project` したプロジェクトに対して 403 になる。全スクリプトで
    「gcloud に設定したアカウント・プロジェクト」をそのまま使うため、トークンは gcloud から取得する。
    HANDSON_USE_ADC=1 を設定すると従来どおり ADC を使用する。
    """

    def __init__(self, quota_project_id: str | None = None):
        super().__init__()
        self._quota_project_id = quota_project_id

    def refresh(self, request) -> None:  # noqa: ARG002 (request は未使用)
        out = subprocess.run(
            ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if not out:
            raise RuntimeError("gcloud auth print-access-token が空でした。'gcloud auth login' を実行してください。")
        self.token = out
        # gcloud のトークン有効期限は約 60 分。余裕を見て 45 分で再取得する
        self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

    def with_quota_project(self, quota_project_id: str) -> "GcloudCredentials":
        return GcloudCredentials(quota_project_id)


def get_credentials(project: str) -> google.auth.credentials.Credentials:
    """ハンズオン共通の認証情報を返す（既定: gcloud アクティブアカウント、HANDSON_USE_ADC=1 で ADC）。"""
    if os.environ.get("HANDSON_USE_ADC", "").strip() == "1":
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds.with_quota_project(project) if hasattr(creds, "with_quota_project") else creds
    try:
        creds = GcloudCredentials(quota_project_id=project)
        creds.refresh(None)
        return creds
    except (OSError, subprocess.CalledProcessError, RuntimeError) as e:
        print(f"⚠️ gcloud の認証情報を取得できなかったため ADC にフォールバックします: {e}")
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds


class RemoteMcpClient:
    def __init__(self, project: str):
        self.project = project
        self.creds = get_credentials(project)
        self._req_id = 0

    def _token(self) -> str:
        if not self.creds.valid:
            self.creds.refresh(google.auth.transport.requests.Request())
        return self.creds.token

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._req_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = requests.post(
            KC_MCP_URL,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "X-Goog-User-Project": self.project,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"MCP HTTP {resp.status_code}: {resp.text[:600]}")

        # Streamable HTTP (SSE or JSON) の両対応パース
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            last_data = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    data_str = line[len("data:") :].strip()
                    if data_str:
                        last_data = json.loads(data_str)
            if last_data is None:
                raise RuntimeError(f"Empty SSE response: {resp.text[:300]}")
            data = last_data
        else:
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"MCP RPC error: {json.dumps(data['error'], ensure_ascii=False)}")
        return data.get("result", {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        res = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            raise RuntimeError(f"Tool error ({name}): {json.dumps(res, ensure_ascii=False)[:600]}")
        # structuredContent があれば優先、なければ content[0].text を JSON パースまたは文字列返却
        if "structuredContent" in res:
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except Exception:
            return joined


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--query", default="予実乖離 計画最新版 ケース換算", help="search_entries に渡す検索クエリ")
    args = ap.parse_args()

    project = resolve_project(args.project)
    mcp = RemoteMcpClient(project)

    print("=" * 78)
    print(f"🔌 Knowledge Catalog リモート MCP 疎通確認 (Endpoint: {KC_MCP_URL})")
    print("=" * 78)

    # 1. tools/list
    tools_res = mcp.rpc("tools/list")
    tools = tools_res.get("tools", [])
    print(f"\n[1] 公開 MCP ツール一覧 ({len(tools)} 個):")
    for t in tools:
        desc_first = (t.get("description") or "").strip().splitlines()[0] if t.get("description") else ""
        print(f"  - {t['name']:<18}: {desc_first[:75]}")

    # 2. search_entries
    print(f"\n[2] MCP `search_entries` 実行 (query='{args.query}'):")
    search_out = mcp.call_tool(
        "search_entries",
        {"projectId": project, "query": args.query, "pageSize": 10},
    )
    results = search_out.get("results", []) if isinstance(search_out, dict) else []
    print(f"  ヒット件数: {len(results)} 件")
    for r in results[:8]:
        de = r.get("dataplexEntry", {})
        es = de.get("entrySource", {})
        name = de.get("name", "")
        disp = es.get("displayName") or name.split("/")[-1]
        sys_name = es.get("system") or ("BigQuery" if "@bigquery" in name else "Glossary/Context")
        print(f"  - [{sys_name:<16}] {disp}  ({name.split('/entries/')[-1]})")

    # 3. lookup_context
    bq_resources = [
        f"projects/{project}/locations/{LOCATION}/entryGroups/@bigquery/entries/"
        f"bigquery.googleapis.com/projects/{project}/datasets/{DATASET_ID}/tables/{tbl}"
        for tbl in ("ZMATMST", "ZPLAN_BUILDUP", "ZSHIP")
    ]
    print(f"\n[3] MCP `lookup_context` 実行 (対象: ZMATMST, ZPLAN_BUILDUP, ZSHIP):")
    ctx_out = mcp.call_tool(
        "lookup_context",
        {"projectId": project, "location": LOCATION, "resources": bq_resources},
    )
    ctx_text = ctx_out.get("context", "") if isinstance(ctx_out, dict) else str(ctx_out)
    print("-" * 78)
    if ctx_text.strip():
        print(ctx_text.strip())
    else:
        print("(コンテキストが空です。Enrichment Proposals の Approve または `python fast_forward_kc.py` を実行してください)")
    print("-" * 78)
    print("💡 上記の YAML が 1 リクエストで AI エージェントに供給され、ハルシネーションを防ぎます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
