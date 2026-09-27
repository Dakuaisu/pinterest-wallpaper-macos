#!/bin/bash

# @raycast.schemaVersion 1
# @raycast.title Rotate Current Pin
# @raycast.mode silent
# @raycast.packageName Pinterest Wallpaper
# @raycast.argument1 { "type": "dropdown", "placeholder": "Direction", "optional": true, "data": [{"title": "Clockwise", "value": "cw"}, {"title": "Counter-clockwise", "value": "ccw"}, {"title": "180", "value": "180"}, {"title": "Reset", "value": "reset"}] }

exec "$(dirname "$0")/../../pw" rotate "${1:-cw}"
