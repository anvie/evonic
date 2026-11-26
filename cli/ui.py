"""Small terminal styling helpers for the CLI wizards (no dependencies).

Colour is used only when stdout is a terminal and NO_COLOR is not set, so piped
output and logs stay plain.
"""

import datetime
import os
import re
import shutil
import sys

_ANSI = re.compile(r"\033\[[0-9;]*m")

ACCENT = "38;5;105"  # indigo, matches the dashboard
OK = "32"
WARN = "33"
ERR = "31"


def enabled() -> bool:
    return (
        sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


def style(text, *codes) -> str:
    if not enabled() or not codes:
        return str(text)
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def accent(text):
    return style(text, ACCENT)


def bold(text):
    return style(text, "1")


def dim(text):
    return style(text, "2")


def ok(text):
    return style(text, OK)


def warn(text):
    return style(text, WARN)


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def home_short(path: str) -> str:
    """/Users/me/.evonic/x -> ~/.evonic/x"""
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home):]
    return path


def fit(text: str, width: int) -> str:
    """Shorten *text* from the left with an ellipsis so it fits in *width*."""
    if width <= 1 or len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


SEP = "\0sep"  # a card line that draws a divider


def card(lines, title=None, indent=2, min_width=44) -> str:
    """Rounded box around *lines* (which may contain ANSI styling).

    The box is as wide as its widest line, but never wider than the terminal.
    """
    cols = shutil.get_terminal_size((100, 24)).columns
    inner_max = max(20, cols - indent - 4)
    body = list(lines)
    if title:
        body = [bold(title), ""] + body
    width = min(inner_max, max([min_width] + [visible_len(l) for l in body if l != SEP]))
    pad = " " * indent
    out = [f"{pad}{dim('╭' + '─' * (width + 2) + '╮')}"]
    edge = dim("│")
    for line in body:
        if line == SEP:
            out.append(f"{pad}{dim('├' + '─' * (width + 2) + '┤')}")
            continue
        gap = max(0, width - visible_len(line))
        out.append(f"{pad}{edge} {line}{' ' * gap} {edge}")
    out.append(f"{pad}{dim('╰' + '─' * (width + 2) + '╯')}")
    return "\n".join(out)


def kv(label: str, value: str, label_width: int = 11) -> str:
    return f"{dim(label.ljust(label_width))}{value}"


# ── Message of the day ───────────────────────────────────────────────────────
# One gradient per weekday (top, bottom). The colour of the day is a small
# tradition of the Evonic banner.
_DAY_GRADIENTS = [
    ("#ff7a7a", "#c0264f"),  # Monday    coral → crimson
    ("#e08bff", "#6d3bd2"),  # Tuesday   orchid → violet
    ("#7be0a4", "#1d8f6a"),  # Wednesday mint → emerald
    ("#ffd76a", "#e0802b"),  # Thursday  gold → amber
    ("#7fb2ff", "#4a4fe0"),  # Friday    sky → indigo
    ("#7ff0ff", "#2a8fd6"),  # Saturday  aqua → ocean
    ("#ffe9a0", "#e0a42b"),  # Sunday    cream → honey
]

# Real commands, one per day, so the banner teaches something.
TIPS = [
    ("evonic chat", "talk to an agent right in the terminal"),
    ("evonic doctor --fix", "check the install and repair what it can"),
    ("evonic backup", "save a full backup archive"),
    ("evonic pass", "change the admin password"),
    ("evonic update", "move to the latest release"),
    ("evonic agent list", "see your agents"),
    ("evonic model list", "see the configured LLM models"),
    ("evonic plugin list", "see installed plugins"),
    ("evonic status", "check whether the server is running"),
    ("/", "search every setting in the dashboard"),
]

_ART = r"""
         ░░░░░░░░░░░░░░░░░░░░░░░░
       ░░▒▒███████████████████▒▒░░
     ░░▒▒██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██▒▒░░      ___________                  .__.
     ░░▒▒██▓█████████████████▓██▒▒░░      \_   _____/__  ______   ____ |__| _____
     ░░▒▒██▓█████ ██  ██ ████▓██▒▒░░       |    __)_\  \/ /    \ /    \|  |/ ____\
     ░░▒▒██▓█████████████████▓██▒▒░░       |        \\   (  ()  )   |  \  \  \____
     ░░▒▒███████████████████████▒▒░░      /_______  / \_/ \____/|___|  /__|\___  /
       ░░▒▒░░░░░░░░░░░░░░░░░░▒▒░░                 \/                 \/        \/
        ▓▓ ░░▓▓ ░░ ▓▓ ░░▓▓ ░░▓▓
      ▒▒ ░░ ▒▒ ▓▓  ▒▒  ▓▓   ▒▒▒
        ░░ ░▒░  ▓▓  ▒▒  ▓▓  ░▓
          ▒▒ ▒▒░▒▒  ▒▒░░▒  ▒▒
            ░░    ▓▓▓▓    ▓▓
              ▒▒        ▒▒
"""


def _hex(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _fg(rgb):
    r, g, b = rgb
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return f"\033[38;2;{r};{g};{b}m"
    idx = 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)
    return f"\033[38;5;{idx}m"


def _gradient(lines, top, bottom):
    n = max(1, len(lines) - 1)
    out = []
    for i, line in enumerate(lines):
        t = i / n
        rgb = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        out.append(f"{_fg(rgb)}{line}\033[0m" if line.strip() else line)
    return out


def tip_of_the_day(today=None):
    today = today or datetime.date.today()
    return TIPS[today.toordinal() % len(TIPS)]


def motd(version="", status=None, url=None, extra=None, tip=True, today=None) -> str:
    """The banner: gradient mascot and wordmark, then an info card.

    *status* is "running", "stopped" or None; *extra* is a list of ready-made
    card lines (e.g. host and port when the server is starting).
    """
    today = today or datetime.date.today()
    art = _ART.strip("\n").split("\n")
    if enabled():
        top, bottom = (_hex(c) for c in _DAY_GRADIENTS[today.weekday()])
        art = _gradient(art, top, bottom)
    out = ["", *art, ""]

    head = bold("Evonic") + (" " + dim("v" + version) if version else "")
    if status == "running":
        right = ok("●") + " running"
    elif status == "stopped":
        right = dim("○ stopped")
    else:
        right = ""
    body = []
    if url and status == "running":
        body.append(accent(url))
    elif status == "stopped":
        body.append(dim("Start it with ") + accent("evonic start -d"))
    body.extend(extra or [])
    if tip:
        cmd, what = tip_of_the_day(today)
        body += [SEP, f"{dim('tip')}  {accent(cmd)} {dim('· ' + what)}"]
    # Right-align the status against the widest line of the card.
    cols = shutil.get_terminal_size((100, 24)).columns
    width = min(cols - 8, max([52, visible_len(head) + visible_len(right) + 2]
                              + [visible_len(l) for l in body if l != SEP]))
    gap = max(2, width - visible_len(head) - visible_len(right))
    lines = [head + " " * gap + right] + body
    out.append(card(lines, min_width=width))
    out.append("")
    return "\n".join(out)
