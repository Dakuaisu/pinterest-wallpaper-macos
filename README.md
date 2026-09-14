# pinterest-wallpaper-macos

Rotates your macOS desktop wallpaper through the pins on a public Pinterest board.

A `launchd` agent changes the wallpaper every 15 minutes and re-reads the board once a day, so new pins join the rotation and unpinned ones leave it. Images are rendered to your display's native resolution rather than letting macOS upscale them.

No Pinterest API key or login required — it reads the public board page.

## Requirements

- macOS (uses `launchd`, `osascript`, and `system_profiler`)
- Python 3.9+
- `requests` and `Pillow`

```sh
pip3 install --user requests Pillow
```

## Install

```sh
git clone https://github.com/Dakuaisu/pinterest-wallpaper-macos.git ~/PinterestWallpaper
cd ~/PinterestWallpaper
chmod +x pw wallpaper_switcher.sh

cp config.example.json config.json
# edit config.json and point "boards" at your board
```

Fetch the pins and set the first wallpaper:

```sh
./pw refresh
./pw next
```

macOS will ask for permission to control System Events the first time. That prompt has to be accepted or the wallpaper can't be changed.

To rotate automatically, install the agent:

```sh
sed "s|/Users/YOUR_USERNAME|$HOME|g" com.pinterest.wallpaper.plist.example \
  > com.pinterest.wallpaper.plist
ln -sf ~/PinterestWallpaper/com.pinterest.wallpaper.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pinterest.wallpaper.plist
```

To remove it:

```sh
launchctl bootout gui/$(id -u)/com.pinterest.wallpaper
rm ~/Library/LaunchAgents/com.pinterest.wallpaper.plist
```

## Commands

```
./pw status     show boards, resolution, what's next, when the next sync is due
./pw next       show the next wallpaper
./pw prev       go back to the previous one
./pw shuffle    reshuffle the deck and jump to a new wallpaper
./pw refresh    re-read the boards from Pinterest right now
./pw ban        drop the current wallpaper from rotation, then advance
./pw unban      clear the ban list
./pw tick       refresh if due, then advance (what launchd runs)
```

`ban` excludes an image without deleting it, so a later sync won't bring it back.

## Configuration

`config.json`, created from `config.example.json`:

| Key | Default | What it does |
| --- | --- | --- |
| `boards` | placeholder | One or more public board URLs. Pins from all of them share one rotation. |
| `refresh_hours` | `24` | How often to re-read the boards. |
| `retry_hours` | `1` | How long to wait before retrying after a failed sync. |
| `resolution` | `"auto"` | `"auto"` detects your display, or set `"3456x2234"` explicitly. |
| `fit_mode` | `"cover"` | `"cover"` crops to fill the screen. `"blur"` fits the whole image with a blurred backdrop. |
| `sharpen` | `true` | Unsharp mask when an image is upscaled. Subtle. |
| `menubar_gradient` | `false` | Dark gradient along the top edge so menu-bar text stays readable. |
| `notifications` | `true` | Notify on new pins, and after three consecutive failures. |
| `new_pins_first` | `true` | Show newly added pins before resuming the normal order. |

Editing `config.json` re-renders existing wallpapers on the next sync, so changes to `fit_mode`, `sharpen` and friends take effect without deleting anything.

## How it works

Pinterest builds board pages in the browser, so the HTML has no plain list of images. It does embed the page's Redux state in a `__PWS_INITIAL_PROPS__` script tag, and the `BoardFeedResource` entry inside it lists exactly the pins on the board — no ads, no suggestions. Those get downloaded to `images/`, cropped to your screen size into `wallpapers/`, and named after a hash of the source URL so nothing is fetched or rendered twice.

Rotation uses a shuffle bag: every wallpaper is shown once, in random order, before any repeats.

Two details that matter more than they look:

- **Deletion is keyed off the board listing, not off what downloaded.** A pin that fails to download looks identical to an unpinned one, so deriving the keep-list from successful downloads means a network blip can wipe the collection. The listing is the source of truth instead.
- **If the page scrape breaks, it falls back to the board's RSS feed** and skips pruning for that run, because the feed may not list the whole board.

State lives in `.state.json`. `images/` and `wallpapers/` are caches — deleting them is safe, the next sync rebuilds them.

## Caveats

- This reads a page Pinterest never promised to keep stable. The RSS fallback covers a markup change, but a full redesign would need the parsing updated. Failures are logged to `agent.err` with a real message rather than failing silently.
- Only the first page of the board feed is read, roughly 25 pins. Larger boards rotate through their most recent pins.
- Boards must be public.
- Downloaded images belong to whoever posted them. This is a personal wallpaper tool, not a redistribution one.

## Logs

`agent.log` gets a line per wallpaper change, `agent.err` gets failures. Neither is rotated; they grow a few MB a year.
