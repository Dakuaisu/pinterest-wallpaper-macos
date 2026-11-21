# Pinwall reference

[← Back to the README](../README.md)

Rotates your macOS desktop wallpaper through the pins on one or more public Pinterest boards.

A `launchd` agent changes the wallpaper on a timer (every 15 minutes by default) and re-reads the boards once a day, so new pins join the rotation and unpinned ones leave it. Each wallpaper is rendered for the dimensions of each display; a small source image can still require enlargement.

No Pinterest API key or login required — it reads the public board page.

**Why:** you already collect the images you love on Pinterest; this puts them on your desktop without maintaining a wallpaper folder by hand.

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
./pw pause [30m|2h|1d]         stop automatic changes, optionally for a duration
./pw resume                    restart automatic changes
./pw tui                       open the Pinwall control center

./pw ban [display]             drop the pin on screen from rotation, then advance
./pw fav [display]             toggle favorite; favorites come up more often
./pw rotate [cw|ccw|180|reset|auto] [display]
                                turn the pin on screen, e.g. so a vertical pin fills a landscape display
./pw open [display]            open the pin on screen on pinterest.com
./pw orient preview [PIN_ID]   print a prediction without caching it
./pw orient scan               cache predictions for up to four pins

./pw refresh                   re-read the boards from Pinterest now
./pw bans                      list banned pins
./pw unban <pin-id>|all        put banned pins back into rotation
./pw status                    boards, displays, what's showing, what's next, agent state

./pw install                   install and load the launchd agent
./pw uninstall                 remove the launchd agent
./pw doctor                    diagnose setup problems
./pw tick                      refresh if due, then change the wallpaper (what launchd runs)
```

Bans, favorites and rotations are stored per pin, so they survive re-renders, display changes and Pinterest URL changes. A rotation is remembered for that pin every time it comes up.

A manual `next`/`prev` isn't overridden by the agent straight away: the next scheduled change is skipped if you changed the wallpaper within the last half interval. Scheduled changes are also skipped while the screen is locked (`skip_when_locked`), so you don't miss wallpapers nobody saw.

## Pinwall control center

Run `./pw tui` from the project directory. It needs an interactive terminal and no extra dependencies. The control center needs at least 36 columns by 10 rows; below that size it asks you to resize and only quit is available.

Everything happens on one screen: your wallpapers on the left, a live preview of the selected one on the right, and the keys you can press along the bottom. Moving through the list never changes your desktop.

| Key | What it does |
| --- | --- |
| `↑` `↓` (or `k` `j`), `PgUp` `PgDn`, `Home` `End` | Move through the list |
| `Enter` | Put the selected wallpaper on your desktop |
| `Space` | Add it to or remove it from favorites |
| `r` | Rotate a quarter turn; the preview updates straight away |
| `f` | Open the live framing editor; `m` switches proportional and stretch modes |
| `F` | Toggle the legacy fill setting and clear any custom framing for this pin |
| `c` | Edit crop focus on the whole photo; arrow keys move it, `0` resets it, `Enter` keeps the draft, and `Esc` cancels this edit |
| `]` | Cycle the display used for the preview (also works in the focus and framing editors) |
| `v` | Compare with the untouched original |
| `x` | Ban it (asks first) |
| `/`, `*` | Search by size as you type (try `1920`), show favorites only. `Esc` clears them |
| `n` `p` `S` `P` `R` | Next, previous, shuffle, pause or resume, refresh boards |
| `+` | Add a board: paste its Pinterest link, and its pins are fetched straight away |
| `b` | Boards: `Enter` shows only that board, `d` switches it off or back on, `x` removes it (asks first), `+` adds one |
| `a` | More actions: pause for a while, banned wallpapers, ban or open what's on screen, setup |
| `,` / `i` / `?` | Settings / status / keyboard help |
| `q` | Quit (or leave the view you're in) |

In the list, `★` marks a favorite, `●` the wallpaper on screen now, and `~` an adjustment you haven't saved. Each row shows the pin's size and its dominant colour, since pins have no titles.

The focus editor shows the whole rotated photo with a marker and an outline of the crop for the selected display. The arrows move in the direction shown on screen, even after rotation. `0` removes the manual focus and returns to the configured automatic or center crop. Keeping the edit with `Enter` only updates the draft: press `s` to save it without changing the desktop, or press `Enter` again from the browser to apply it. `Esc` discards only the current focus edit and keeps any earlier draft. Focus affects cover crops; an image that fits without cropping may not visibly move. Switching displays changes the preview only, not which displays receive the wallpaper when you apply it.

In the **framing editor**, `↑`/`↓` change proportional zoom in 5% steps, and `←`/`→` cycle the frame aspect between display, original, 1:1, 4:3, 3:2, 16:9, 9:16, and 21:9. At 100% the photo covers the selected frame; zooming in crops more, while zooming out can expose the backdrop. Press `m` for **stretch** mode: `←`/`→` adjust width and `↑`/`↓` adjust height independently in 5% steps. Stretch can distort the photo. Zoom, width, and height are limited to 25–400%. The editor previews the selected display live and retains the current rotation and crop focus.

Press `0` in the editor to return this pin to legacy framing, `Enter` to stage the edit, or `Esc` to cancel only this editing session (earlier staged edits remain). From the browser, `s` saves the selected pin's staged adjustments without changing the desktop; `Enter` applies that wallpaper and saves its adjustments. Drafts on other pins remain unsaved. `F` is the legacy fill toggle: it clears custom framing while retaining other staged adjustments. Existing wallpapers without custom framing continue to use the previous fit/fill behavior; resetting or clearing framing restores that behavior.

In **Settings**, `Enter` flips an on/off setting immediately, or opens the value for editing (`Ctrl-U` clears the field, `Enter` saves, `Esc` cancels). Saving writes `config.json` but doesn't refresh boards; changing `interval minutes` still needs the agent reinstalled from **More**.

A board that is switched off stays in your list and keeps its downloaded images, but its wallpapers leave the rotation and it isn't re-read from Pinterest until you switch it back on. A pin that is also on another board that is still on stays in rotation. Removing a board deletes its downloaded images; your last board can't be removed.

Only bans, removing a board and installing or removing the background agent ask for confirmation: `y` confirms, anything else cancels. One background action runs at a time and its result appears on the line above the keys; quitting doesn't cancel an action that has already started.

### Preview quality

How sharp the preview is depends on the terminal:

- **Ghostty, Kitty, iTerm2 and WezTerm** show the real image at full resolution. Nothing to configure.
- **VS Code's terminal** shows the real image only when `terminal.integrated.enableImages` is on and `terminal.integrated.gpuAcceleration` is `auto` or `on`; Pinwall checks for about 100 ms at startup. If VS Code shows inline images elsewhere but Pinwall doesn't pick it up, run `PINTEREST_WALLPAPER_IMAGE=iterm ./pw tui` from the project directory.
- **Everything else** (Apple Terminal, tmux, screen, VS Code without images) gets a picture drawn from coloured block characters, four "pixels" per character. It's a good likeness, not a sharp one; a smaller font or a bigger window gives it more to work with. If your font draws the quarter-block characters badly, `PINTEREST_WALLPAPER_IMAGE=half` uses plain half-blocks, and `PINTEREST_WALLPAPER_IMAGE=blocks` forces block characters even where real images would work.

Block characters use truecolor except in Apple Terminal, where they use 256 colors unless `COLORTERM` explicitly says `truecolor` or `24bit`. Late, complete query replies are filtered from keyboard input. If a terminal sends a malformed reply that never terminates, printable keys can remain buffered even after the initial wait: press an arrow key to resynchronize, then retry the action, or press Ctrl-C to exit. Keys entered while the reply was incomplete may be discarded.

## Configuration

`config.json` is created from `config.example.json`. Unknown keys are ignored; invalid values fall back to defaults rather than stopping the tool. Normal command runs report configuration problems in `agent.err`.

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
| `fill_pins` | `[]` | Pin IDs that always use `cover` with no `max_upscale` limit, regardless of those global render settings. For example: `["123456789"]`. The `skip_upscale_over` filter still applies. |
| `crop` | `"smart"` | Where `cover` crops from. `"smart"` slides the crop towards the side with the most detail when one side clearly has more; otherwise it stays centred. `"center"` always crops evenly. |
| `max_upscale` | `0` | `0` = no limit. E.g. `2.5`: a pin that would need more enlargement than that is shown smaller, centred on the backdrop, instead of stretched blurry. |
| `backdrop` | `"blur"` | Background behind fitted images: `"blur"` (blurred copy) or `"color"` (the pin's main colour). |
| `sharpen` | `true` | Unsharp mask when an image is enlarged. Subtle. |
| `menubar_gradient` | `false` | Dark gradient along the top edge so menu-bar text stays readable. |
| `upscaler` | `""` | Path to [`realesrgan-ncnn-vulkan`](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) to AI-upscale pins needing more than 1.5× enlargement. Runs on two pins per tick, after the wallpaper changes, so it never delays rotation. Keep its `models/` folder next to the binary. Failed upscaling does not remove a pin from rotation; delete `upscaled/` to retry it. |
| `upscaler_model` | `"realesrgan-x4plus"` | Model passed to the upscaler with `-n`. |
| `orientation_model` | `""` | Local ONNX model path. Empty disables [automatic orientation](#automatic-orientation-optional). |
| `different_per_display` | `false` | Show a different pin on each display instead of the same one everywhere. |
| `favorite_weight` | `2` | How many times a favorite appears per pass through the deck (spread out, never back to back). |
| `mood` | `"off"` | `"appearance"`: darker pins in Dark Mode, lighter in Light Mode. `"night"`: darker pins during `night_hours`, lighter otherwise. |
| `night_hours` | `[19, 7]` | Start and end hour of "night" for `mood: "night"`. |
| `skip_when_locked` | `true` | Don't change the wallpaper while the screen is locked. |
| `notifications` | `true` | Notify on new pins, and when a board fails three refreshes in a row. macOS shows them as coming from Script Editor; if none ever appear, allow Script Editor in System Settings → Notifications. |
| `new_pins_first` | `true` | Show newly added pins before resuming the normal order. |

Render settings (`fit_mode`, `fill_pins`, `crop`, `max_upscale`, `backdrop`, `sharpen`, `menubar_gradient`, `upscaler`, `orientation_model`, rotations) affect subsequent renders.

`./pw status` and the menu bar show each pin's source size and how much it's being enlarged (e.g. `735x418 source, scaled 5.3x to fill 3456x2234`), and `./pw doctor` counts the pins that need more than 2× — a hint to set `max_upscale`, `skip_upscale_over` or an upscaler.

## Automatic orientation (optional)

Automatic orientation predicts corrections for sideways or upside-down pins when rendering; a prediction is not proof of the intended orientation. It runs locally on your Mac, uploads no images, and never modifies pin source files in `images/`. If the model is unsure, it abstains and the pin renders as-is unless you've set a manual rotation.

1. From the project directory, install ONNX Runtime and NumPy into the Python that `pw` uses. Run the command for your setup:

   ```sh
   .venv/bin/python3 -m pip install onnxruntime numpy
   # If you skipped the venv, use this instead:
   /usr/bin/python3 -m pip install --user onnxruntime numpy
   ```

   On Python 3.9, you can use `'onnxruntime==1.19.2' 'numpy<2'` in place of `onnxruntime numpy`.

2. From the [pinned upstream v2 release](https://github.com/duartebarbosadev/deep-image-orientation-detection/releases/tag/v2), download the exact model and check its SHA-256:

   ```sh
   mkdir -p models
   curl -fL 'https://github.com/duartebarbosadev/deep-image-orientation-detection/releases/download/v2/orientation_model_v2_0.9882.onnx' -o models/orientation.onnx
   shasum -a 256 models/orientation.onnx
   ```

   The file must be 80,578,424 bytes and the checksum must be `cffe911c1dff47fbfbbd90110aaab9c07134645c460d35b3ae8832079bea91ba`. `pw` checks both before inference.
3. Set `"orientation_model": "models/orientation.onnx"` in your existing `config.json`. Relative paths are resolved from the directory containing `config.json`. You don't need to reinstall the launchd agent for this edit.
4. Run `./pw orient preview` to print the model's clockwise correction (or `abstain`) and score for the first pin recorded as current, or the last history entry if none is current. Use `./pw orient preview PIN_ID` for another downloaded pin. Preview neither saves the prediction nor changes the wallpaper. If you don't have any downloaded pins yet, run `./pw refresh` first and provide a pin ID.
5. Run `./pw orient scan` to cache results for up to four downloaded, unbanned pins that need scanning. Repeat to cover more pins. With a cold cache, setting the model path alone doesn't apply corrections: run a scan or let an eligible tick scan after a successful wallpaper change. Results take effect on subsequent renders, not on the wallpaper already showing.

**Override:** `./pw rotate cw`, `./pw rotate ccw`, or `./pw rotate 180` turns the pin on screen from its current angle and saves a per-pin override. With auto orientation enabled, `./pw rotate reset` forces no extra rotation; `./pw rotate auto` clears the override and falls back to any cached prediction. Append a display number to target another display.

**Disable:** Set `"orientation_model": ""` in `config.json`. Subsequent renders ignore automatic predictions but still honor manual rotations. No launchd reinstall is needed.

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

[MIT](../LICENSE)
