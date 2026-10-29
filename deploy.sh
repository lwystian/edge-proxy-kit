#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
command -v python3 >/dev/null || { echo '请先安装 Python 3.8+。' >&2; exit 1; }
exec python3 "$script_dir/edge.py" "$@"
