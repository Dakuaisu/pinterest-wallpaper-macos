#!/bin/bash

# @raycast.schemaVersion 1
# @raycast.title Ban Current Pin
# @raycast.mode silent
# @raycast.packageName Pinterest Wallpaper

exec "$(dirname "$0")/../../pw" ban
