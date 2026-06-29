#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${BQUANT_SESSION:-bquant_live_worker}"

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "running: ${SESSION_NAME}"
  tmux list-panes -t "${SESSION_NAME}" -F '#{pane_pid} #{pane_current_command} #{pane_active}'
else
  echo "not running: ${SESSION_NAME}"
fi
