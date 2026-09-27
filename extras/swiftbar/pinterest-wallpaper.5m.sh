#!/usr/bin/env bash
# <xbar.title>Pinterest Wallpaper</xbar.title>
# <xbar.desc>Next, previous, favorite, rotate, ban and pause for pinterest-wallpaper-macos.</xbar.desc>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>

src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
  dir="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  [[ $src != /* ]] && src="$dir/$src"
done

exec "$(cd -P "$(dirname "$src")/../.." && pwd)/pw" menubar
