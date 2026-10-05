# pinterest-wallpaper-macos

[![CI](https://github.com/Dakuaisu/pinterest-wallpaper-macos/actions/workflows/ci.yml/badge.svg)](https://github.com/Dakuaisu/pinterest-wallpaper-macos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![The wallpaper changing, the SwiftBar menu, and a Raycast command](docs/demo.gif)

Rotates your macOS desktop wallpaper through the pins on one or more public Pinterest boards.

A `launchd` agent changes the wallpaper on a timer (every 15 minutes by default) and re-reads the boards once a day, so new pins join the rotation and unpinned ones leave it. Each wallpaper is rendered at the native resolution of each display rather than letting macOS upscale it.

No Pinterest API key or login required — it reads the public board page.

**Why:** you already collect the images you love on Pinterest; this puts them on your desktop at full resolution, without maintaining a wallpaper folder by hand.

## Requirements

- macOS (uses `launchd`, `osascript` and AppKit through JavaScript for Automation)
- Python 3.9+
- `requests` and `Pillow` 9.1+

## Install

```sh
git clone https://github.com/Dakuaisu/pinterest-wallpaper-macos.git ~/PinterestWallpaper
cd ~/PinterestWallpaper

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.json config.json
# edit config.json and point "boards" at your board

./pw doctor      # checks Python, displays, the wallpaper API and your boards
./pw refresh     # fetch the pins
./pw next        # set the first wallpaper
./pw install     # start the launchd agent
```

`pw` uses `.venv` when it exists and falls back to `/usr/bin/python3` otherwise. If you skip the venv, install into that interpreter specifically — a bare `pip3` may belong to a different Python:

```sh
/usr/bin/python3 -m pip install --user -r requirements.txt
```

`./pw install` writes `~/Library/LaunchAgents/io.github.dakuaisu.pinterest-wallpaper.plist` and loads it. Run it again after changing `interval_minutes`. It also removes the agent from older versions (`com.pinterest.wallpaper`). `./pw uninstall` removes it.

## Commands

```
./pw next                      show the next wallpaper
./pw prev                      go back to the previous one
./pw shuffle                   reshuffle the deck and jump to a new wallpaper
./pw pause [30m|2h|1d]         stop automatic changes (until ./pw resume without a duration)
./pw resume                    restart automatic changes

./pw ban [display]             drop the pin on screen from rotation, then advance
./pw fav [display]             toggle favorite; favorites come up more often
./pw rotate [cw|ccw|180|reset] [display]
                               turn the pin on screen, e.g. so a vertical pin fills a landscape display
./pw open [display]            open the pin on screen on pinterest.com

./pw refresh                   re-read the boards from Pinterest now
./pw bans                      list banned pins
./pw unban <pin-id>|all        put banned pins back into rotation
./pw status                    boards, displays, what's showing, what's next, agent state

./pw install / uninstall       manage the launchd agent
./pw doctor                    diagnose setup problems
./pw tick                      refresh if due, then change the wallpaper (what launchd runs)
```

Bans, favorites and rotations are stored per pin, so they survive re-renders, display changes and Pinterest URL changes. A rotation is remembered for that pin every time it comes up.

A manual `next`/`prev` isn't overridden by the agent straight away: the next scheduled change is skipped if you changed the wallpaper within the last half interval. Scheduled changes are also skipped while the screen is locked (`skip_when_locked`), so you don't miss wallpapers nobody saw.

## Configuration

`config.json`, created from `config.example.json`. Unknown keys and wrong types are reported in `agent.err` and replaced by the default, rather than stopping the tool.

| Key | Default | What it does |
| --- | --- | --- |
| `boards` | placeholder | One or more public board URLs. Pins from all of them share one rotation. |
| `interval_minutes` | `15` | How often the wallpaper changes. Re-run `./pw install` after changing it. |
| `refresh_hours` | `24` | How often to re-read the boards. |
| `retry_hours` | `1` | How long to wait before retrying after a failed sync. Being offline doesn't count; that retries on the next tick. |
| `max_pins` | `250` | Most pins read per board (older ones are paged in until this cap). |
| `skip_video` | `true` | Leave video pins out; their cover frame rarely makes a good wallpaper. |
| `skip_upscale_over` | `0` | `0` = off. E.g. `3`: pins that would need more than 3× enlargement to fill your largest display are left out, and aren't even downloaded when the board reports their size. |
| `resolution` | `"auto"` | `"auto"` renders for each display's own size, or set `"3456x2234"` for all of them. |
| `fit_mode` | `"cover"` | `"cover"` crops to fill the screen. `"blur"` fits the whole image on a backdrop. |
| `crop` | `"smart"` | Where `cover` crops from. `"smart"` slides the crop towards the side with the most detail when one side clearly has more; otherwise it stays centred. `"center"` always crops evenly. |
| `max_upscale` | `0` | `0` = no limit. E.g. `2.5`: a pin that would need more enlargement than that is shown smaller, centred on the backdrop, instead of stretched blurry. |
| `backdrop` | `"blur"` | Background behind fitted images: `"blur"` (blurred copy) or `"color"` (the pin's main colour). |
| `sharpen` | `true` | Unsharp mask when an image is enlarged. Subtle. |
| `menubar_gradient` | `false` | Dark gradient along the top edge so menu-bar text stays readable. |
| `upscaler` | `""` | Path to [`realesrgan-ncnn-vulkan`](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) to AI-upscale pins needing more than 1.5× enlargement. Runs on two pins per tick, after the wallpaper changes, so it never delays rotation. Keep its `models/` folder next to the binary. Pins it fails on are skipped; delete `upscaled/` to retry. |
| `upscaler_model` | `"realesrgan-x4plus"` | Model passed to the upscaler with `-n`. |
| `different_per_display` | `false` | Show a different pin on each display instead of the same one everywhere. |
| `favorite_weight` | `2` | How many times a favorite appears per pass through the deck (spread out, never back to back). |
| `mood` | `"off"` | `"appearance"`: darker pins in Dark Mode, lighter in Light Mode. `"night"`: darker pins during `night_hours`, lighter otherwise. |
| `night_hours` | `[19, 7]` | Start and end hour of "night" for `mood: "night"`. |
| `skip_when_locked` | `true` | Don't change the wallpaper while the screen is locked. |
| `notifications` | `true` | Notify on new pins, and when a board fails three refreshes in a row. macOS shows them as coming from Script Editor; if none ever appear, allow Script Editor in System Settings → Notifications. |
| `new_pins_first` | `true` | Show newly added pins before resuming the normal order. |

Render settings (`fit_mode`, `crop`, `max_upscale`, `backdrop`, `sharpen`, `menubar_gradient`, `upscaler`, rotations) take effect on the next wallpaper change; other keys never cause a re-render.

`./pw status` and the menu bar show each pin's source size and how much it's being enlarged (e.g. `735x418 source, scaled 5.3x to fill 3456x2234`), and `./pw doctor` counts the pins that need more than 2× — a hint to set `max_upscale`, `skip_upscale_over` or an upscaler.

## Menu bar and Raycast

- **SwiftBar or xbar**: link the plugin into your plugin folder, e.g.
  `ln -s ~/PinterestWallpaper/extras/swiftbar/pinterest-wallpaper.5m.sh "<your plugin folder>/"`.
  The menu has next/previous/shuffle, favorite, rotate, ban, open, pause/resume and refresh. It is printed by `./pw menubar`.
- **Raycast**: add `extras/raycast` as a script-command folder (Raycast → Extensions → Script Commands → Add Directories). Each command can be given its own hotkey.

## Multiple displays

Each display gets its own render at its own size and aspect ratio. With `different_per_display`, each shows a different pin; add the display number (as listed by `./pw status`) to `ban`, `fav`, `rotate` and `open` to act on a display other than the main one.

If you use several Spaces, macOS only changes the wallpaper of the Space that's active on each display. Turn on "Show on all Spaces" in System Settings → Wallpaper to have every Space follow along.

## How it works

Pinterest builds board pages in the browser, so the HTML has no plain list of images. It does embed the page's Redux state in a `__PWS_INITIAL_PROPS__` script tag; its `BoardFeedResource` entry lists the board's first page of pins (no ads, no suggestions) and a bookmark for the next page, fetched from Pinterest's `BoardFeedResource/get` endpoint until `max_pins`. Originals are downloaded to `images/` unchanged (named by pin ID) and rendered into `wallpapers/` on demand, one file per pin, display size and render settings.

Rotation uses a shuffle bag: every pin is shown once, in random order, before any repeats (favorites a few times, spread out).

Details that matter more than they look:

- **Deletion is keyed off the board listing, not off what downloaded.** A pin that fails to download looks identical to an unpinned one, so the listing is the source of truth. A board that fails, is read through RSS, or can't be paged to the end keeps all its pins for that run.
- **If the page scrape breaks, it falls back to the board's RSS feed.** Network errors skip that fallback and aren't counted as failures — an offline laptop won't warn you that your board is gone.
- **Everything is written atomically and commands take a lock**, so a shutdown mid-download can't leave a half-written image, and a `./pw ban` during a scheduled refresh isn't lost.
- **Big boards aren't re-paged when nothing changed.** If the pin count, the board's last-reordered time and the first page all match the previous refresh, the stored listing is reused instead of fetching every page again.
- **`wallpapers/` stays small.** A refresh keeps renders only for recent, upcoming and on-screen pins; anything else is re-rendered when it comes up (well under a second).

State lives in `.state.json` (a corrupt one is moved aside, not silently reset). `images/`, `upscaled/` and `wallpapers/` are caches — deleting them is safe; run `./pw refresh` to rebuild them straight away rather than at the next sync.

## Caveats

- This reads a page and an endpoint Pinterest never promised to keep stable. The RSS fallback covers a markup change, but a full redesign would need the parsing updated.
- Boards must be public.
- Downloaded images belong to whoever posted them. This is a personal wallpaper tool, not a redistribution one.

## Logs and tests

`agent.log` gets a line per wallpaper change and refresh; `agent.err` gets failures and config problems. Neither is rotated; they grow a few MB a year.

```sh
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
