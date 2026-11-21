# Wallpaper browser: terminal design contract

## Control-center research and decisions
- [Yazi features](https://yazi-rs.github.io/features/): keep the selected item
  next to a generous preview; search incrementally without generating a grid of
  thumbnails. The existing bounded, asynchronous preview remains authoritative.
- [Zellij compact bar](https://zellij.dev/documentation/compact-bar-alias.html):
  fit navigation and current mode in compact, persistent chrome; show relevant
  keys at the bottom rather than dedicating a pane to help.
- [Textual command palette](https://textual.textualize.io/guide/command_palette/):
  expose named actions immediately, filter them while typing, and use arrow keys
  and Enter to choose. This is a design reference, not a dependency.

## Control-center tokens and primitives
- **One screen.** The library list and its preview are home; there are no tabs.
  Boards, search and favorites are filters on that list. Settings, status, help
  and the More menu open as a single full-width panel over it and close with
  Esc or `q`, returning to the same row and filters.
- **Readable rows.** Pins have no titles, so a row shows its number, a swatch of
  the pin's dominant colour, and its pixel size. Opaque pin IDs never appear in
  the list. `★` favorite, `●` on screen now and `~` unsaved adjustment are
  separate markers from the reverse-video selection.
- **Surface/text:** terminal default background and foreground. One accent
  (bold cyan) marks the focused panel and the app name; dim is used only for
  borders and passive status. `NO_COLOR` removes colour but keeps bold, dim and
  reverse video, so nothing depends on colour alone.
- **Frames:** thin rounded boxes with the title in the top edge and the caption
  (filters, or size and adjustments) in the bottom edge.
- **Keys:** arrows and `j`/`k` move; Enter acts on the selection; Space toggles
  a favorite; `/` searches as you type; `?` is help; Esc backs out one level.
  The bottom row lists the keys for the current view and drops whole entries,
  never half a label, when the terminal is narrow; More, Help and Quit always
  remain. Letters typed into a prompt are text, never shortcuts.
- **Move, see, apply.** Moving never changes the desktop. `r` and `f` change the
  preview only. Enter applies the wallpaper and remembers those adjustments;
  `s` remembers them without applying.
- **Feedback** appears on the row above the keys: the running action by name,
  then "done" or a one-line failure. Longer output and Status open the output
  panel. An open panel is never replaced by a finishing action.
- **Boards** are managed from one list: Enter filters, `d` switches a board off
  or on (kept, with its images, but out of rotation and not re-read), `x`
  removes it after confirmation, `+` adds one from a pasted link.
- **Action safety:** only bans, board removal and agent install/removal ask, on that same row,
  and only `y` confirms. A ban of the on-screen wallpaper captures the pin
  before asking and the command re-checks it under the state lock. Displays are
  only asked about when there is more than one. Settings switches flip on
  Enter; other values are edited in place and saved with compare-and-swap.
  Input is never interpreted as a shell command.
- **Adaptive layout:** the list takes about a third of the width (16–38 cells)
  and the image the rest. At 36×10 both panels, the position, the image and the
  essential keys remain; the caption shortens before it truncates. Below 36×10
  only a resize/quit notice is shown and keys other than quit are ignored.

## Direction
An image-first, quiet workspace: the photograph supplies the color and the
terminal supplies its own font and background. No external branding, animation,
or decorative artwork. The intended user is browsing with a keyboard, including
in a resized or monochrome-configured terminal.

## Structure and tokens
- Clip external text by terminal cell width, accounting for wide and combining
  characters, and strip control sequences. Empty collections and recoverable
  errors have readable text in place of an image.
- Redraw from the current state after a resize.

## Image and interaction
- Display exactly the backend's preview, preserving its physical aspect ratio.
  Use reported terminal pixel geometry when available; otherwise assume a cell
  is twice as tall as it is wide. Transparent pixels are composited on black.
- Generate previews in the background at bounds supplied by the terminal for
  the actual image box; lightweight test screens retain a compatible fallback. Show
  `Loading preview...` while the current preview is pending, keeping keyboard
  input responsive. Only the current preview result may be displayed.
- Terminals that identify themselves get real images without a query: Kitty
  and Ghostty through Kitty PNG graphics, iTerm2 and WezTerm through inline
  images. The character fallback draws two by two pixels per cell with quarter
  blocks, choosing the best two colours per cell, and keeps exact top and bottom
  colours where a cell has little contrast; `PINTEREST_WALLPAPER_IMAGE=half`
  restores plain half-blocks.
- Use direct Kitty PNG graphics for a positively identified, unwrapped Kitty
  terminal. In VS Code, send one Kitty graphics query after entering cbreak and
  wait about 100 ms: a matching success enables PNG, while silence or an error
  uses upper-half blocks. Do not query in Apple Terminal, under tmux or screen,
  or when `PINTEREST_WALLPAPER_IMAGE=blocks` forces the fallback. Blocks use
  truecolor except in Apple Terminal, where 256 colors are used unless
  `COLORTERM` explicitly declares `truecolor` or `24bit`.
- Preserve keyboard input around the query and filter complete matching replies,
  including late and fragmented replies. An unterminated graphics response can
  hold printable input indefinitely after the probe timeout; a new arrow-key
  escape sequence resynchronizes the parser, potentially discarding held input.
  Ctrl-C can also exit. This limit avoids treating a delayed reply as commands.
- Drafts belong to individual pins; `q` discards the ones that were neither
  applied nor saved. The preview caption shows effective settings and whether
  the selected pin has unsaved changes.
- Use the alternate screen, restore terminal modes and cursor on every normal
  or exceptional exit, and delete only images owned by this session.

## Accessibility and accepted limits
The UI is keyboard-only, with explicit selection and text labels. Inline blocks
need UTF-8; the 256-color fallback approximates image colors, while truecolor
blocks are more faithful. Cell geometry is approximate when the terminal does
not report pixel dimensions; screenshots and assistive interpretation vary by
terminal and font. This is a terminal surface, not a web page; browser-specific
sizing, typography, and motion do not apply.
