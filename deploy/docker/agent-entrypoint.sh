#!/bin/sh
# Agent container entrypoint (design D4, D5).
#  1. Render $CODEX_HOME/config.toml from env (full_access + never, request_user_input off, providers).
#  2. With arguments, run them instead (e.g. `codex login --device-auth`).
#  3. Otherwise run agentd (restarted if it dies) and the stdio-over-TCP relay to `codex app-server`.
set -eu

: "${CODEX_HOME:=/home/agent/.codex}"
: "${APP_SERVER_PORT:=4500}"
: "${AGENTD_PORT:=4501}"
export CODEX_HOME

python -m codex_im.agent_config

PATH_DIR="$(python -c 'from codex_cli_bin import bundled_path_dir; d = bundled_path_dir(); print(d or "")')"
if [ -n "$PATH_DIR" ]; then
  PATH="$PATH_DIR:$PATH"
  export PATH
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

if [ -z "${CODEX_IM_AGENTD_TOKEN:-}" ]; then
  echo "CODEX_IM_AGENTD_TOKEN is required" >&2
  exit 2
fi

(
  while true; do
    python -m codex_im.agentd --listen "0.0.0.0:${AGENTD_PORT}" || true
    echo "agentd exited; restarting in 2s" >&2
    sleep 2
  done
) &

# One app-server per connection; the bridge keeps a single connection (D5).
# VERIFY(ubuntu): V0 — behaviour of an in-flight turn when the bridge connection drops.
exec socat "TCP-LISTEN:${APP_SERVER_PORT},reuseaddr,fork" "EXEC:/usr/local/bin/codex-app-server"
