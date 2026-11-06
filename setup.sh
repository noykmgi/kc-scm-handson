#!/usr/bin/env bash
# Knowledge Catalog & Knowledge Studio 60分ハンズオン 環境構築スクリプト (所要約 2〜3 分)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# プロジェクト ID の解決順序: HANDSON_PROJECT_ID（明示指定）> gcloud config > Cloud Shell 環境変数
# ※ Cloud Shell は起動時に GOOGLE_CLOUD_PROJECT を自動注入するため、gcloud config set project の変更を優先させる
PROJECT_ID="${HANDSON_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${DEVSHELL_PROJECT_ID:-}}"
fi
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "❌ エラー: プロジェクトが未設定です。 'gcloud config set project <PROJECT_ID>' を実行してください。"
  exit 1
fi
# 以降の Python スクリプト（generate_bq_data.py 等）にも同じプロジェクトを引き渡す
export HANDSON_PROJECT_ID="${PROJECT_ID}"
export GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"

LOCATION="us-central1"
SA_NAME="kc-handson-enrichment-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DOCS_BUCKET="${PROJECT_ID}-kc-handson-docs"
EVAL_BUCKET="${PROJECT_ID}-kc-handson-eval"

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [[ "${ACCOUNT}" == *.gserviceaccount.com ]]; then
  CALLER_MEMBER="serviceAccount:${ACCOUNT}"
else
  CALLER_MEMBER="user:${ACCOUNT}"
fi

echo "========================================================================"
echo "🚀 Knowledge Catalog & Studio ハンズオン セットアップ開始"
echo "   Project  : ${PROJECT_ID}"
echo "   Location : ${LOCATION}"
echo "   Caller   : ${CALLER_MEMBER}"
echo "========================================================================"

# 課金アカウント未リンクのプロジェクトでは API 有効化や GCS バケット作成が 403 で失敗するため、先に確認して停止する
BILLING_ENABLED="$(gcloud billing projects describe "${PROJECT_ID}" --format="value(billingEnabled)" 2>/dev/null || echo "unknown")"
if [[ "${BILLING_ENABLED}" == "False" ]]; then
  echo "❌ エラー: プロジェクト ${PROJECT_ID} に課金アカウントがリンクされていません。"
  echo "   コンソール [お支払い] から課金アカウントをリンクした後、再度 setup.sh を実行してください（再実行は安全です）:"
  echo "   https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT_ID}"
  exit 1
fi

echo ""
echo "▶ [Step 1/6] 必要な Google Cloud API を有効化しています..."
gcloud services enable \
  bigquery.googleapis.com \
  dataplex.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  datalineage.googleapis.com \
  geminidataanalytics.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}" --quiet

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
DATAPLEX_AGENT="service-${PROJECT_NUMBER}@gcp-sa-dataplex.iam.gserviceaccount.com"

# Dataplex Service Agent が未生成の新規プロジェクト向けに明示生成
gcloud beta services identity create --service=dataplex.googleapis.com --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true

echo ""
echo "▶ [Step 2/6] Knowledge Studio Enrichment 実行用サービスアカウントと IAM 権限を構成しています..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="KC Hands-on Enrichment Execution SA" \
    --project="${PROJECT_ID}" --quiet
fi

# 新規作成直後のサービスアカウントは IAM 側への伝播に数十秒かかる場合があるため、参照可能になるまで待機する
echo "  ⏳ サービスアカウントの伝播を待機しています..."
for i in $(seq 1 30); do
  if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# IAM バインディング付与（伝播遅延による "does not exist" / 同時更新による競合は指数バックオフで再試行）
iam_bind() {
  # 使い方: iam_bind <project|sa> <リソース> <メンバー> <ロール>
  local kind="$1" resource="$2" member="$3" role="$4" attempt
  for attempt in 1 2 3 4 5 6; do
    if [[ "${kind}" == "project" ]]; then
      gcloud projects add-iam-policy-binding "${resource}" \
        --member="${member}" --role="${role}" --condition=None --quiet >/dev/null 2>/tmp/kc_iam_err && return 0
    else
      gcloud iam service-accounts add-iam-policy-binding "${resource}" \
        --member="${member}" --role="${role}" --project="${PROJECT_ID}" --condition=None --quiet >/dev/null 2>/tmp/kc_iam_err && return 0
    fi
    sleep $((attempt * 5))
  done
  echo "  ❌ IAM 付与に失敗しました: ${member} → ${role} (${resource})"
  cat /tmp/kc_iam_err
  return 1
}

# Execution SA へのロール付与（Public Preview で分離された roles/dataplex.contentEditor を含む）
for ROLE in \
  roles/serviceusage.serviceUsageConsumer \
  roles/bigquery.dataViewer \
  roles/dataplex.catalogEditor \
  roles/dataplex.catalogViewer \
  roles/dataplex.contentEditor \
  roles/dataplex.workflowApprover; do
  iam_bind project "${PROJECT_ID}" "serviceAccount:${SA_EMAIL}" "${ROLE}"
done

# Dataplex Service Agent が Execution SA を Impersonate し、BQ プロファイリングを行えるよう権限付与
iam_bind sa "${SA_EMAIL}" "serviceAccount:${DATAPLEX_AGENT}" "roles/iam.serviceAccountTokenCreator"

for ROLE in roles/bigquery.dataViewer roles/bigquery.resourceViewer; do
  iam_bind project "${PROJECT_ID}" "serviceAccount:${DATAPLEX_AGENT}" "${ROLE}"
done

# ハンズオン実施者本人への権限付与（Owner 以外の環境でも確実に動くよう Dataplex Content ロール群等を明示付与）
iam_bind sa "${SA_EMAIL}" "${CALLER_MEMBER}" "roles/iam.serviceAccountUser" || true

for ROLE in \
  roles/dataplex.dataScanEditor \
  roles/dataplex.catalogAdmin \
  roles/dataplex.contentCreator \
  roles/dataplex.contentAdmin \
  roles/dataplex.workflowApprover \
  roles/datalineage.viewer \
  roles/bigquery.jobUser \
  roles/serviceusage.serviceUsageConsumer \
  roles/mcp.toolUser \
  roles/geminidataanalytics.dataAgentStatelessUser; do
  iam_bind project "${PROJECT_ID}" "${CALLER_MEMBER}" "${ROLE}" || true
done
echo "  ✅ サービスアカウント構成完了: ${SA_EMAIL}"

echo ""
echo "▶ [Step 3/6] GCS バケットを作成し、業務引き継ぎメモ (SOP) と評価データセットを配置しています..."
for BKT in "${DOCS_BUCKET}" "${EVAL_BUCKET}"; do
  if ! gcloud storage buckets describe "gs://${BKT}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${BKT}" \
      --project="${PROJECT_ID}" \
      --location="${LOCATION}" \
      --uniform-bucket-level-access --quiet
  fi
done

gcloud storage cp "${SCRIPT_DIR}/inputs/scm_operation_notes.md" "gs://${DOCS_BUCKET}/scm_operation_notes.md" --quiet

TMP_EVAL="$(mktemp)"
sed "s/__PROJECT_ID__/${PROJECT_ID}/g" "${SCRIPT_DIR}/inputs/eval_template.jsonl" > "${TMP_EVAL}"
gcloud storage cp "${TMP_EVAL}" "gs://${EVAL_BUCKET}/scm_eval.jsonl" --quiet
rm -f "${TMP_EVAL}"

gcloud storage buckets add-iam-policy-binding "gs://${DOCS_BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectViewer" --quiet >/dev/null

for ROLE in roles/storage.objectViewer roles/storage.objectCreator; do
  gcloud storage buckets add-iam-policy-binding "gs://${EVAL_BUCKET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" --quiet >/dev/null
done
echo "  ✅ Knowledge Signals 用バケット : gs://${DOCS_BUCKET}"
echo "  ✅ Evaluation Set 用ファイル   : gs://${EVAL_BUCKET}/scm_eval.jsonl"

echo ""
echo "▶ [Step 4/6] Python 仮想環境 (.venv) を準備しています..."
if [[ -n "${HANDSON_VENV:-}" && -x "${HANDSON_VENV}/bin/python" ]]; then
  VENV_DIR="${HANDSON_VENV}"
elif [[ -x "${SCRIPT_DIR}/../../.venv/bin/python" ]]; then
  VENV_DIR="$(cd "${SCRIPT_DIR}/../../.venv" && pwd)"
else
  VENV_DIR="${SCRIPT_DIR}/.venv"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}"
  fi
  "${VENV_DIR}/bin/pip" install --upgrade pip -q
  "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" -q
fi
echo "  ✅ 使用仮想環境: ${VENV_DIR}"

echo ""
echo "▶ [Step 5/6] BigQuery ミニ SCM データセットと初期カタログシードを投入しています..."
"${VENV_DIR}/bin/python" "${SCRIPT_DIR}/generate_bq_data.py" --project="${PROJECT_ID}" --seed-catalog --verify-golden

echo ""
echo "▶ [Step 6/6] セットアップが完了しました。Step 1 の Knowledge Studio 画面で以下の設定値を入力してください:"
echo "========================================================================"
echo "【Knowledge Studio 設定パラメータ一覧】"
echo "  ・Knowledge Studio URL    : https://console.cloud.google.com/dataplex/studio?project=${PROJECT_ID}"
echo "  ・Location (リージョン)   : ${LOCATION}"
echo "  ・Context Set ID          : kc-handson-scm-context"
echo "  ・Resources to enrich (3テーブル一括指定用):"
echo "    //bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/kc_handson_scm/tables/ZMATMST,//bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/kc_handson_scm/tables/ZPLAN_BUILDUP,//bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/kc_handson_scm/tables/ZSHIP"
echo "  ・Knowledge signals (GCS) : ${DOCS_BUCKET}  (※ Cloud Storage buckets, folders, or files 欄)"
echo "  ・Knowledge signals (用語): projects/${PROJECT_ID}/locations/${LOCATION}/glossaries/kc-handson-scm-glossary"
echo "  ・Evaluation set          : gs://${EVAL_BUCKET}/scm_eval.jsonl"
echo "  ・Run as > Service account: ${SA_EMAIL}"
echo ""
echo "※ 以降の Python コマンドは以下の仮想環境を有効化して実行してください:"
echo "   source ${VENV_DIR}/bin/activate"
echo "========================================================================"
