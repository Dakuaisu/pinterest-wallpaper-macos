#!/usr/bin/env bash
# Turn a screen recording (.mov from Cmd+Shift+5) into an optimized GIF for the README.
#
#   scripts/make_demo_gif.sh recording.mov [docs/demo.gif]
#
# Optional env: START=2 (seconds to skip), DURATION=28 (seconds to keep), MAX_MB=5.

set -euo pipefail

usage() {
    echo "usage: $0 <recording.mov> [output.gif]" >&2
    echo "env:   START=<sec> DURATION=<sec> MAX_MB=<size limit, default 5>" >&2
    exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
input=$1
output=${2:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docs/demo.gif"}
max_bytes=$(( ${MAX_MB:-5} * 1000 * 1000 ))

[[ -f $input ]] || { echo "error: $input not found" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "error: ffmpeg not found (brew install ffmpeg)" >&2; exit 1; }

trim=()
[[ -n ${START:-} ]] && trim+=(-ss "$START")
[[ -n ${DURATION:-} ]] && trim+=(-t "$DURATION")

mkdir -p "$(dirname "$output")"
tmp=$(mktemp -t demo-gif.XXXXXX)
trap 'rm -f "$tmp"' EXIT

# Largest first; stop at the first width/fps pair that fits.
for attempt in 960:15 800:15 800:12 720:12 640:12 640:10 560:10 480:10; do
    width=${attempt%:*}
    fps=${attempt#*:}
    echo "Trying ${width}px wide at ${fps} fps..."

    # One palette for the whole clip; diff mode favours what moves, rectangle limits re-dithering to changed areas.
    ffmpeg -hide_banner -loglevel error -y ${trim[@]+"${trim[@]}"} -i "$input" -an -f gif -filter_complex \
        "fps=$fps,scale=$width:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
        "$tmp"

    if command -v gifsicle >/dev/null; then
        gifsicle -O3 --lossy=40 -b "$tmp" 2>/dev/null || true
    fi

    size=$(wc -c <"$tmp" | tr -d ' ')
    echo "  -> $(( size / 1000 )) KB"

    if (( size <= max_bytes )); then
        mv "$tmp" "$output"
        trap - EXIT
        echo "Wrote $output (${width}px, ${fps} fps, $(( size / 1000 )) KB)"
        exit 0
    fi
done

echo "error: still over ${MAX_MB:-5} MB at 480px/10fps; trim the clip with START/DURATION" >&2
exit 1
