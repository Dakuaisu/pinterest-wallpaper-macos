from __future__ import annotations

from typing import NamedTuple


class Action(NamedTuple):
    label: str
    command: str
    args: tuple[str, ...] = ()
    kind: str = "job"
    key: str = ""
    confirm: str = ""


NEXT = Action("Next wallpaper", "next", key="n")
PREVIOUS = Action("Previous wallpaper", "prev", key="p")
SHUFFLE = Action("Shuffle", "shuffle", key="S")
PAUSE = Action("Pause or resume", "pause", kind="pause", key="P")
REFRESH = Action("Refresh boards", "refresh", key="R")
ADD_BOARD = Action("Add a board…", "board", kind="add-board", key="+")
BOARDS = Action("Boards…", "boards", kind="boards", key="b")
FAVORITES = Action("Show favorites only", "favorites", kind="favorites", key="*")
SETTINGS = Action("Settings", "settings", kind="settings", key=",")
STATUS = Action("Status", "status", kind="status", key="i")
HELP = Action("Keyboard help", "help", kind="help", key="?")

# Section titles are plain strings; the cursor skips them.
MENU: tuple[Action | str, ...] = (
    "Rotation",
    NEXT,
    PREVIOUS,
    SHUFFLE,
    PAUSE,
    Action("Pause for a while…", "pause", kind="duration"),
    "Library",
    REFRESH,
    ADD_BOARD,
    BOARDS,
    FAVORITES,
    Action("Banned wallpapers…", "unban", kind="bans"),
    "On screen now",
    Action("Ban the wallpaper on screen…", "ban", kind="screen"),
    Action("Open it on Pinterest", "open", kind="screen"),
    "Setup",
    SETTINGS,
    STATUS,
    Action("Check setup", "doctor", kind="report"),
    Action("Detect sideways wallpapers", "orient", ("scan",), "report"),
    Action("Start changing wallpapers automatically…", "install", kind="confirm",
           confirm="Install the background agent that changes the wallpaper on a timer?"),
    Action("Stop changing wallpapers automatically…", "uninstall", kind="confirm",
           confirm="Remove the background agent? Wallpapers will stop changing on their own."),
    Action("Show the last command's output", "output", kind="output"),
    HELP,
)

SHORTCUTS = {action.key: action for action in MENU if isinstance(action, Action) and action.key}

HELP_TEXT = (
    "Browse",
    "  ↑ ↓  or  k j        Move through your wallpapers",
    "  PgUp PgDn Home End  Jump further",
    "  /                   Search by size, like 1920",
    "  b                   Boards: show one, switch one off, remove or add",
    "  *                   Show favorites only",
    "  Esc                 Clear search and filters",
    "This wallpaper",
    "  Enter               Put it on your desktop",
    "  Space               Add to or remove from favorites",
    "  r                   Rotate a quarter turn",
    "  f                   Edit framing; m switches proportional / stretch",
    "  F                   Toggle legacy fill and clear custom framing",
    "  ↑ ↓ ← → (framing)  Zoom / ratio, or stretch width / height",
    "  0 (framing)         Reset to legacy; ] previews the next display",
    "  c                   Edit crop focus; arrows move it, 0 resets",
    "  ]                   Preview the next display",
    "  Enter / Esc (edit)  Keep the draft / cancel this edit",
    "  s                   Remember adjustments without applying",
    "  v                   Compare with the untouched original",
    "  x                   Ban: never show it again",
    "Rotation",
    "  n  p                Next or previous wallpaper",
    "  S                   Shuffle",
    "  P                   Pause or resume automatic changes",
    "  R                   Fetch new pins from your boards",
    "  +                   Add another Pinterest board",
    "  d  x  (in Boards)   Switch a board off or on; remove it",
    "Everything else",
    "  a                   More actions",
    "  ,                   Settings",
    "  i                   Status",
    "  q                   Quit",
    "",
    "In the list:  ★ favorite   ● on screen now   ~ adjustments not saved yet",
)
