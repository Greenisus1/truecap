#!/bin/bash
# pi-app-store: 1
set -eu
cd -- "$(dirname -- "$0")"
case "${1:-}" in
  install)
    command -v python3 >/dev/null || { echo 'Python 3.8 or newer is required'; exit 1; }
    python3 -c 'import sys; assert sys.version_info >= (3, 8), "Python 3.8 or newer is required"'
    python3 truecap.py --help >/dev/null
    echo 'truecap ready. Run lists drives only; no test starts automatically.' ;;
  run) shift;exec python3 fullscreen.py "$@" ;;
  *) echo 'Use: bash app-store.sh install OR bash app-store.sh run'; exit 1 ;;
esac
