#!/usr/bin/env bash
# Knowledge Catalog & Knowledge Studio ハンズオン リソース一括クリーンアップスクリプト
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

AUTO_YES=false
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
  AUTO_YES=true
fi

# プロジェクト ID の解決順序: HANDSON_PROJECT_ID（明示指定）> gcloud config > Cloud Shell 環境変数
# ※ Cloud Shell は起動時に GOOGLE_CLOUD_PROJECT を自動注入するため、gcloud config set project の変更を優先させる
PROJECT_ID="${HANDSON_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${DEVSHELL_PROJECT_ID:-}}"
fi
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "❌ エラー: プロジェクトが未設定です。"
  exit 1
fi

SA_EMAIL="kc-handson-enrichment-sa@${PROJECT_ID}.iam.gserviceaccount.com"
DOCS_BUCKET="gs://${PROJECT_ID}-kc-handson-docs"
EVAL_BUCKET="gs://${PROJECT_ID}-kc-handson-eval"

echo "========================================================================"
echo "🧹 【ドライラン確認】以下のハンズオン用リソースを削除します (Project: ${PROJECT_ID})"
echo "   1. Knowledge Catalog : EntryGroups (kc-handson-scm-context, aws-rds-scm)"
echo "                          Glossary (kc-handson-scm-glossary)"
echo "                          AspectType (kc-handson-business-rule)"
echo "   2. BigQuery Dataset  : ${PROJECT_ID}:kc_handson_scm (ZMATMST, ZPLAN_BUILDUP, ZSHIP)"
echo "   3. GCS Buckets       : ${DOCS_BUCKET}, ${EVAL_BUCKET}"
echo "   4. Service Account   : ${SA_EMAIL}"
echo "   ※ 既存の他データセットやリソースには一切影響しません。"
echo "========================================================================"

if [[ "${AUTO_YES}" != "true" ]]; then
  read -r -p "本当に削除を実行しますか？ [y/N]: " ans
  if [[ ! "${ans}" =~ ^[Yy]$ ]]; then
    echo "キャンセルしました。"
    exit 0
  fi
fi

if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
  PY="${SCRIPT_DIR}/.venv/bin/python"
elif [[ -x "${SCRIPT_DIR}/../../.venv/bin/python" ]]; then
  PY="${SCRIPT_DIR}/../../.venv/bin/python"
else
  PY="python3"
fi

echo "▶ [1/4] Knowledge Catalog リソースを削除中..."
"${PY}" "${SCRIPT_DIR}/fast_forward_kc.py" --project="${PROJECT_ID}" --cleanup || true

echo "▶ [2/4] BigQuery データセット kc_handson_scm を削除中..."
bq rm -r -f -d "${PROJECT_ID}:kc_handson_scm" >/dev/null 2>&1 || true

echo "▶ [3/4] GCS バケットを削除中..."
for BKT in "${DOCS_BUCKET}" "${EVAL_BUCKET}"; do
  if gcloud storage buckets describe "${BKT}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage rm --recursive "${BKT}" --quiet >/dev/null 2>&1 || true
  fi
done

echo "▶ [4/4] Enrichment 用サービスアカウントを削除中..."
if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts delete "${SA_EMAIL}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
fi

echo ""
echo "✅ クリーンアップ完了！"
echo "   ※ コンソール UI で手動作成した Enrichment Task がある場合は、"
echo "      https://console.cloud.google.com/dataplex/studio?project=${PROJECT_ID} の [Enrichments] から Delete してください。"
