#!/usr/bin/env bash
set -Eeuo pipefail
source_root="$(cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$source_root/install.py" "$@"
