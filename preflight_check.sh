#!/usr/bin/env bash
# Knowledge Catalog & Knowledge Studio ハンズオン 事前環境チェックスクリプト (所要約 10 秒)
set -euo pipefail

# プロジェクト ID の解決順序: HANDSON_PROJECT_ID（明示指定）> gcloud config > Cloud Shell 環境変数
# ※ Cloud Shell は起動時に GOOGLE_CLOUD_PROJECT を自動注入するため、gcloud config set project の変更を優先させる
PROJECT_ID="${HANDSON_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${DEVSHELL_PROJECT_ID:-}}"
fi
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"

echo "========================================================================"
echo "🔍 Knowledge Catalog & Studio ハンズオン 事前環境チェック"
echo "========================================================================"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "❌ エラー: プロジェクト ID が設定されていません。"
  echo "   実行例: gcloud config set project <YOUR_PROJECT_ID>"
  exit 1
fi

echo "  対象プロジェクト : ${PROJECT_ID}"
echo "  実行アカウント   : ${ACCOUNT}"

# 0. 課金アカウントのリンク確認（未リンクだと GCS バケット作成等が 403 で失敗する）
echo -n "  [0/3] 課金アカウント リンク確認 ... "
BILLING_ENABLED="$(gcloud billing projects describe "${PROJECT_ID}" --format="value(billingEnabled)" 2>/dev/null || echo "unknown")"
if [[ "${BILLING_ENABLED}" == "True" ]]; then
  echo "✅ 有効"
elif [[ "${BILLING_ENABLED}" == "unknown" ]]; then
  echo "⚠️ 確認できません（cloudbilling API 未有効または権限不足。setup.sh 内で再確認します）"
else
  echo "❌ 未リンク"
  echo "     → コンソール [お支払い] (https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT_ID}) から"
  echo "       課金アカウントをリンクしてから setup.sh を実行してください。"
fi

# 1. API 有効化テスト (dataplex.googleapis.com)
echo -n "  [1/3] Dataplex API 有効化確認 ... "
if gcloud services list --enabled --project="${PROJECT_ID}" --filter="config.name=dataplex.googleapis.com" --format="value(config.name)" | grep -q "dataplex.googleapis.com"; then
  echo "✅ 有効"
else
  echo "⏳ 未有効（setup.sh 実行時に自動有効化されます）"
fi

# 2. リモート MCP 疎通テスト
echo -n "  [2/3] Knowledge Catalog リモート MCP 疎通確認 ... "
TOKEN="$(gcloud auth print-access-token 2>/dev/null)"
HTTP_CODE="$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://dataplex.googleapis.com/mcp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' || true)"

if [[ "${HTTP_CODE}" == "200" ]]; then
  echo "✅ 正常 (HTTP 200)"
else
  echo "⚠️ HTTP ${HTTP_CODE} (Dataplex API 有効化後に利用可能になります)"
fi

# 3. Knowledge Studio コンソール URL 案内
echo "  [3/3] Knowledge Studio UI 確認:"
echo "        以下の URL をブラウザで開き、左メニューに [New chat / Context sets / Enrichments] が表示されるかご確認ください:"
echo "        👉 https://console.cloud.google.com/dataplex/studio?project=${PROJECT_ID}"
echo ""
echo "        ※ Knowledge Studio は Public Preview として開放されています（事前申請不要）。"
echo "========================================================================"
