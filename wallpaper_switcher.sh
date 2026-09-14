#!/usr/bin/env bash
# launchd entry point. All logic lives in pinterest_wallpaper.py.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

exec /usr/bin/python3 pinterest_wallpaper.py tick
