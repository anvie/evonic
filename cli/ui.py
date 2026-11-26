"""Small terminal styling helpers for the CLI wizards (no dependencies).

Colour is used only when stdout is a terminal and NO_COLOR is not set, so piped
output and logs stay plain.
"""

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


def card(lines, title=None, indent=2, min_width=44) -> str:
    """Rounded box around *lines* (which may contain ANSI styling).

    The box is as wide as its widest line, but never wider than the terminal.
    """
    cols = shutil.get_terminal_size((100, 24)).columns
    inner_max = max(20, cols - indent - 4)
    body = list(lines)
    if title:
        body = [bold(title), ""] + body
    width = min(inner_max, max([min_width] + [visible_len(l) for l in body]))
    pad = " " * indent
    out = [f"{pad}{dim('╭' + '─' * (width + 2) + '╮')}"]
    edge = dim("│")
    for line in body:
        gap = max(0, width - visible_len(line))
        out.append(f"{pad}{edge} {line}{' ' * gap} {edge}")
    out.append(f"{pad}{dim('╰' + '─' * (width + 2) + '╯')}")
    return "\n".join(out)


def kv(label: str, value: str, label_width: int = 11) -> str:
    return f"{dim(label.ljust(label_width))}{value}"
