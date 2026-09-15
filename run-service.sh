#!/usr/bin/env bash
set -Eeuo pipefail

source_root="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$source_root"
private_root="${MEAL_CONCIERGE_HOME:-${HERMES_HOME:+$HERMES_HOME/meal-concierge}}"
private_root="${private_root:-$HOME/.local/share/meal-concierge}"
config_path="${MEAL_CONCIERGE_CONFIG:-$private_root/config.json}"
state_path="${MEAL_CONCIERGE_STATE:-$private_root/state}"
socket_path="${MEAL_CONCIERGE_SOCKET:-$private_root/service.sock}"
token_path="${MEAL_CONCIERGE_TOKENS:-${HERMES_HOME:+$HERMES_HOME/mcp-tokens}}"
token_path="${token_path:-$private_root/tokens}"
browser_home="${MEAL_CONCIERGE_BROWSER_HOME:-$private_root/browser}"
browser_profile="${MEAL_CONCIERGE_BROWSER_PROFILE:-$browser_home/profile}"
browser_socket_directory="${MEAL_CONCIERGE_BROWSER_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}/meal-concierge-$(id -u)}"

find_runtime_python() {
  local candidate
  for candidate in \
    "${MEAL_CONCIERGE_PYTHON:-}" \
    "$source_root/venv/bin/python" \
    "${HERMES_PYTHON:-}"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "Standalone runtime missing; run install.sh install, or supply MEAL_CONCIERGE_PYTHON" >&2
  return 1
}

python="$(find_runtime_python)"
"$python" -c 'from pathlib import Path; from service import config; import sys; config(Path(sys.argv[1]))' "$config_path"
browser_paths="$("$python" -I "$source_root/browser_prerequisites.py")"
if [[ "$browser_paths" != *$'\n'* ]]; then
  echo "Browser prerequisite resolver returned an invalid result" >&2
  exit 1
fi
agent_browser="${browser_paths%%$'\n'*}"
chromium="${browser_paths#*$'\n'}"

umask 077
mkdir -p "$private_root" "$state_path" "$browser_home" "$browser_profile" "$browser_socket_directory" "$(dirname -- "$socket_path")"
chmod 700 "$private_root" "$state_path" "$browser_home" "$browser_profile" "$browser_socket_directory"

service_args=(
  "$python" "$source_root/service.py"
  --config "$config_path"
  --state "$state_path"
  --tokens "$token_path"
  --socket "$socket_path"
  --agent-uid "$(id -u)"
  --socket-group "$(id -g)"
  --browser-profile "$browser_profile"
  --browser-home "$browser_home"
  --browser-socket-directory "$browser_socket_directory"
  --browser-uid "$(id -u)"
  --browser-gid "$(id -g)"
)

service_args+=(--browser-binary "$agent_browser" --browser-executable "$chromium")

exec "${service_args[@]}"
