#!/bin/bash

# @raycast.schemaVersion 1
# @raycast.title Pause Wallpaper Changes
# @raycast.mode silent
# @raycast.packageName Pinterest Wallpaper
# @raycast.argument1 { "type": "text", "placeholder": "30m, 2h, 1d (empty: until resumed)", "optional": true }

exec "$(dirname "$0")/../../pw" pause ${1:+"$1"}
