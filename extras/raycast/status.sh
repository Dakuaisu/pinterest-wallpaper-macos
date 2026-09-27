#!/bin/bash

# @raycast.schemaVersion 1
# @raycast.title Wallpaper Status
# @raycast.mode fullOutput
# @raycast.packageName Pinterest Wallpaper

exec "$(dirname "$0")/../../pw" status
