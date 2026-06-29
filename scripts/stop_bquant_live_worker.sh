#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${BQUANT_SESSION:-bquant_live_worker}"

if ! tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session not found: ${SESSION_NAME}"
  exit 0
fi

tmux send-keys -t "${SESSION_NAME}" C-c
sleep 2

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  tmux kill-session -t "${SESSION_NAME}"
  echo "killed tmux session: ${SESSION_NAME}"
else
  echo "stopped tmux session: ${SESSION_NAME}"
fi
