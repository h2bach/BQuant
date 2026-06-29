#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${BQUANT_SESSION:-bquant_live_worker}"
CONDA_ENV="${BQUANT_CONDA_ENV:-bquant}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_PATH="${REPO_ROOT}/logs/observability/live_update_worker_tmux.log"

mkdir -p "$(dirname "${LOG_PATH}")"

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${SESSION_NAME}"
  tmux display-message -p -t "${SESSION_NAME}" '#S #{session_created_string}' || true
  exit 0
fi

tmux new-session -d -s "${SESSION_NAME}" \
  "cd '${REPO_ROOT}' && source \"\$(conda info --base)/etc/profile.d/conda.sh\" && conda activate '${CONDA_ENV}' && python -m pipelines.live_update_worker 2>&1 | tee -a '${LOG_PATH}'"

echo "started tmux session: ${SESSION_NAME}"
echo "log: ${LOG_PATH}"
