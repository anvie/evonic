"""Deterministic default avatars generated from a name, in the style of Boring Avatars.

This is a Python port of the avatar algorithms from https://github.com/boringdesigners/boring-avatars
(v2.0.4, MIT License, Copyright (c) 2021 boringdesigners): the same name hash, the same shapes and
the same six variants (marble, beam, pixel, sunset, ring, bauhaus). It runs on the server, so no
request is made to a third-party service and no agent name leaves the deployment.

`generate_avatar_svg("Galactus")` always returns the same SVG; a different name gives a different
one. The markup is built only from numbers and palette colours — user text is used to seed the hash
and never reaches the output (the gradient ids in "sunset" are derived from a hash too).

MIT License notice (boring-avatars):
    Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
    associated documentation files (the "Software"), to deal in the Software without restriction,
    including without limitation the rights to use, copy, modify, merge, publish, distribute,
    sublicense, and/or sell copies of the Software ... The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the Software.
"""

import math
import re

DEFAULT_VARIANT = "initials"
VARIANTS = ("initials", "marble", "beam", "pixel", "sunset", "ring", "bauhaus")
# Boring Avatars' own default palette
DEFAULT_COLORS = ("#92A1C6", "#146A7C", "#F0AB3D", "#C271B4", "#C20D90")

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ── numeric helpers (mirror the JS utilities, including 32-bit integer overflow) ──────────────

def _int32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def _hash_code(name: str) -> int:
    """JS: h = ((h << 5) - h) + charCode; h = h & h; return Math.abs(h)  (UTF-16 code units)."""
    data = (name or "").encode("utf-16-le")
    h = 0
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        h = _int32((_int32(h << 5) - h) + unit)
    return abs(h)


def _digit(number: float, ntn: int) -> int:
    return math.floor((number / (10 ** ntn)) % 10)


def _boolean(number: float, ntn: int) -> bool:
    return _digit(number, ntn) % 2 == 0


def _unit(number: int, rng: float, index: int = 0) -> float:
    value = number % rng
    if index and _digit(number, index) % 2 == 0:
        return -value
    return value


def _color(number: int, colors, rng: int) -> str:
    return colors[number % rng]


def _contrast(hexcolor: str) -> str:
    h = hexcolor[1:] if hexcolor.startswith("#") else hexcolor
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 >= 128 else "#FFFFFF"


def _n(value) -> str:
    """Format a number the way JS does (integers without a decimal point)."""
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def _clean_colors(colors):
    palette = [c for c in (colors or ()) if isinstance(c, str) and _HEX_RE.match(c)]
    return tuple(palette) if palette else DEFAULT_COLORS


# ── SVG plumbing ──────────────────────────────────────────────────────────────────────────────

def _svg(size_box: int, size: str, ident: str, body: str, defs: str = "") -> str:
    return (
        f'<svg viewBox="0 0 {size_box} {size_box}" fill="none" role="img" xmlns="http://www.w3.org/2000/svg" '
        f'width="{size}" height="{size}">'
        f'<mask id="{ident}" maskUnits="userSpaceOnUse" x="0" y="0" width="{size_box}" height="{size_box}">'
        f'<rect width="{size_box}" height="{size_box}" rx="{size_box * 2}" fill="#FFFFFF"/></mask>'
        f'<g mask="url(#{ident})">{body}</g>{defs}</svg>'
    )


# ── variants ──────────────────────────────────────────────────────────────────────────────────

def _bauhaus(name, colors, size, ident):
    SIZE = 80
    num = _hash_code(name)
    rng = len(colors)
    els = []
    for i in range(4):
        n = num * (i + 1)
        els.append({
            "color": _color(num + i, colors, rng),
            "tx": _unit(n, SIZE / 2 - (i + 17), 1),
            "ty": _unit(n, SIZE / 2 - (i + 17), 2),
            "rot": _unit(n, 360),
            "square": _boolean(num, 2),
        })
    e = els
    body = (
        f'<rect width="{SIZE}" height="{SIZE}" fill="{e[0]["color"]}"/>'
        f'<rect x="{_n((SIZE - 60) / 2)}" y="{_n((SIZE - 20) / 2)}" width="{SIZE}" '
        f'height="{SIZE if e[1]["square"] else _n(SIZE / 8)}" fill="{e[1]["color"]}" '
        f'transform="translate({_n(e[1]["tx"])} {_n(e[1]["ty"])}) rotate({_n(e[1]["rot"])} {SIZE // 2} {SIZE // 2})"/>'
        f'<circle cx="{SIZE // 2}" cy="{SIZE // 2}" fill="{e[2]["color"]}" r="{_n(SIZE / 5)}" '
        f'transform="translate({_n(e[2]["tx"])} {_n(e[2]["ty"])})"/>'
        f'<line x1="0" y1="{SIZE // 2}" x2="{SIZE}" y2="{SIZE // 2}" stroke-width="2" stroke="{e[3]["color"]}" '
        f'transform="translate({_n(e[3]["tx"])} {_n(e[3]["ty"])}) rotate({_n(e[3]["rot"])} {SIZE // 2} {SIZE // 2})"/>'
    )
    return _svg(SIZE, size, ident, body)


def _ring(name, colors, size, ident):
    SIZE = 90
    num = _hash_code(name)
    rng = len(colors)
    c = [_color(num + i, colors, rng) for i in range(5)]
    t = [c[0], c[1], c[1], c[2], c[2], c[3], c[3], c[0], c[4]]
    body = (
        f'<path d="M0 0h90v45H0z" fill="{t[0]}"/>'
        f'<path d="M0 45h90v45H0z" fill="{t[1]}"/>'
        f'<path d="M83 45a38 38 0 00-76 0h76z" fill="{t[2]}"/>'
        f'<path d="M83 45a38 38 0 01-76 0h76z" fill="{t[3]}"/>'
        f'<path d="M77 45a32 32 0 10-64 0h64z" fill="{t[4]}"/>'
        f'<path d="M77 45a32 32 0 11-64 0h64z" fill="{t[5]}"/>'
        f'<path d="M71 45a26 26 0 00-52 0h52z" fill="{t[6]}"/>'
        f'<path d="M71 45a26 26 0 01-52 0h52z" fill="{t[7]}"/>'
        f'<circle cx="45" cy="45" r="23" fill="{t[8]}"/>'
    )
    return _svg(SIZE, size, ident, body)


def _pixel(name, colors, size, ident):
    SIZE = 80
    num = _hash_code(name)
    rng = len(colors)
    t = [_color(num % (i + 1), colors, rng) for i in range(64)]
    # cell order used by the original component
    cells = [(0, 0), (20, 0), (40, 0), (60, 0), (10, 0), (30, 0), (50, 0), (70, 0)]
    cells += [(0, y) for y in (10, 20, 30, 40, 50, 60, 70)]
    for x in (20, 40, 60, 10, 30, 50, 70):
        cells += [(x, y) for y in (10, 20, 30, 40, 50, 60, 70)]
    assert len(cells) == 64
    body = ""
    for i, (x, y) in enumerate(cells):
        xa = f' x="{x}"' if x else ""
        ya = f' y="{y}"' if y else ""
        body += f'<rect{xa}{ya} width="10" height="10" fill="{t[i]}"/>'
    return (
        f'<svg viewBox="0 0 {SIZE} {SIZE}" fill="none" role="img" xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
        f'<mask id="{ident}" mask-type="alpha" maskUnits="userSpaceOnUse" x="0" y="0" width="{SIZE}" height="{SIZE}">'
        f'<rect width="{SIZE}" height="{SIZE}" rx="{SIZE * 2}" fill="#FFFFFF"/></mask>'
        f'<g mask="url(#{ident})">{body}</g></svg>'
    )


def _beam(name, colors, size, ident):
    SIZE = 36
    num = _hash_code(name)
    rng = len(colors)
    wrapper_color = _color(num, colors, rng)
    pre_x = _unit(num, 10, 1)
    wtx = pre_x + SIZE / 9 if pre_x < 5 else pre_x
    pre_y = _unit(num, 10, 2)
    wty = pre_y + SIZE / 9 if pre_y < 5 else pre_y
    face = _contrast(wrapper_color)
    bg = _color(num + 13, colors, rng)
    wrot = _unit(num, 360)
    wscale = 1 + _unit(num, SIZE / 12) / 10
    mouth_open = _boolean(num, 2)
    is_circle = _boolean(num, 1)
    eye = _unit(num, 5)
    mouth = _unit(num, 3)
    frot = _unit(num, 10, 3)
    ftx = wtx / 2 if wtx > SIZE / 6 else _unit(num, 8, 1)
    fty = wty / 2 if wty > SIZE / 6 else _unit(num, 7, 2)
    if mouth_open:
        mouth_el = f'<path d="M15 {_n(19 + mouth)}c2 1 4 1 6 0" stroke="{face}" fill="none" stroke-linecap="round"/>'
    else:
        mouth_el = f'<path d="M13,{_n(19 + mouth)} a1,0.75 0 0,0 10,0" fill="{face}"/>'
    body = (
        f'<rect width="{SIZE}" height="{SIZE}" fill="{bg}"/>'
        f'<rect x="0" y="0" width="{SIZE}" height="{SIZE}" '
        f'transform="translate({_n(wtx)} {_n(wty)}) rotate({_n(wrot)} {SIZE // 2} {SIZE // 2}) scale({_n(wscale)})" '
        f'fill="{wrapper_color}" rx="{SIZE if is_circle else _n(SIZE / 6)}"/>'
        f'<g transform="translate({_n(ftx)} {_n(fty)}) rotate({_n(frot)} {SIZE // 2} {SIZE // 2})">'
        f'{mouth_el}'
        f'<rect x="{_n(14 - eye)}" y="14" width="1.5" height="2" rx="1" stroke="none" fill="{face}"/>'
        f'<rect x="{_n(20 + eye)}" y="14" width="1.5" height="2" rx="1" stroke="none" fill="{face}"/>'
        f'</g>'
    )
    return _svg(SIZE, size, ident, body)


def _sunset(name, colors, size, ident):
    SIZE = 80
    num = _hash_code(name)
    rng = len(colors)
    c = [_color(num + i, colors, rng) for i in range(4)]
    g0, g1 = f"gradient_paint0_linear_{ident}", f"gradient_paint1_linear_{ident}"
    body = (
        f'<path fill="url(#{g0})" d="M0 0h80v40H0z"/>'
        f'<path fill="url(#{g1})" d="M0 40h80v40H0z"/>'
    )
    defs = (
        '<defs>'
        f'<linearGradient id="{g0}" x1="40" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">'
        f'<stop stop-color="{c[0]}"/><stop offset="1" stop-color="{c[1]}"/></linearGradient>'
        f'<linearGradient id="{g1}" x1="40" y1="40" x2="40" y2="80" gradientUnits="userSpaceOnUse">'
        f'<stop stop-color="{c[2]}"/><stop offset="1" stop-color="{c[3]}"/></linearGradient>'
        '</defs>'
    )
    return _svg(SIZE, size, ident, body, defs)


def _marble(name, colors, size, ident):
    SIZE = 80
    num = _hash_code(name)
    rng = len(colors)
    els = []
    for i in range(3):
        n = num * (i + 1)
        els.append({
            "color": _color(num + i, colors, rng),
            "tx": _unit(n, SIZE / 10, 1),
            "ty": _unit(n, SIZE / 10, 2),
            "scale": 1.2 + _unit(n, SIZE / 20) / 10,
            "rot": _unit(n, 360, 1),
        })
    e = els
    flt = f"filter_{ident}"
    body = (
        f'<rect width="{SIZE}" height="{SIZE}" fill="{e[0]["color"]}"/>'
        f'<path filter="url(#{flt})" d="M32.414 59.35L50.376 70.5H72.5v-71H33.728L26.5 13.381l19.057 27.08L32.414 59.35z" '
        f'fill="{e[1]["color"]}" transform="translate({_n(e[1]["tx"])} {_n(e[1]["ty"])}) '
        f'rotate({_n(e[1]["rot"])} {SIZE // 2} {SIZE // 2}) scale({_n(e[2]["scale"])})"/>'
        f'<path filter="url(#{flt})" style="mix-blend-mode:overlay" '
        f'd="M22.216 24L0 46.75l14.108 38.129L78 86l-3.081-59.276-22.378 4.005 12.972 20.186-23.35 27.395L22.215 24z" '
        f'fill="{e[2]["color"]}" transform="translate({_n(e[2]["tx"])} {_n(e[2]["ty"])}) '
        f'rotate({_n(e[2]["rot"])} {SIZE // 2} {SIZE // 2}) scale({_n(e[2]["scale"])})"/>'
    )
    defs = (
        f'<defs><filter id="{flt}" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">'
        '<feFlood flood-opacity="0" result="BackgroundImageFix"/>'
        '<feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"/>'
        '<feGaussianBlur stdDeviation="7" result="effect1_foregroundBlur"/></filter></defs>'
    )
    return _svg(SIZE, size, ident, body, defs)



# Calm, flat tones from the app palette (indigo/slate family) used by the "initials" variant.
_INITIAL_TONES = ("#6366F1", "#4F46E5", "#7C83F5", "#475569", "#0F766E", "#7C3AED")


def _initials(name, colors, size, ident):
    """Flat circle with the agent's first letter. Only an alphanumeric character from the name is ever emitted."""
    SIZE = 80
    ch = next((c for c in name if c.isalnum()), "?").upper()
    if not (ch.isalnum() or ch == "?"):
        ch = "?"
    bg = _INITIAL_TONES[_hash_code(name) % len(_INITIAL_TONES)]
    body = (
        f'<rect width="{SIZE}" height="{SIZE}" fill="{bg}"/>'
        f'<text x="{SIZE // 2}" y="{SIZE // 2}" fill="#FFFFFF" text-anchor="middle" dominant-baseline="central" '
        f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" font-size="{int(SIZE * 0.46)}" '
        f'font-weight="600">{ch}</text>'
    )
    return _svg(SIZE, size, ident, body)


_RENDERERS = {
    "initials": _initials,
    "marble": _marble, "beam": _beam, "pixel": _pixel,
    "sunset": _sunset, "ring": _ring, "bauhaus": _bauhaus,
}


def generate_avatar_svg(name: str, variant: str = None, colors=None, size: str = "80", square: bool = False) -> str:
    """SVG (circle-masked, or square when `square`) for `name`. Unknown variants fall back to the default; colours must be #RRGGBB."""
    normalized = (name or "").strip().lower() or "?"
    render = _RENDERERS.get(variant or DEFAULT_VARIANT) or _RENDERERS[DEFAULT_VARIANT]
    palette = _clean_colors(colors)
    # hash-derived (+ variant): safe to place in id attributes and unique when several are inlined in one page
    ident = "av" + format(_hash_code(normalized), "x") + "-" + (variant if variant in _RENDERERS else DEFAULT_VARIANT)
    svg = render(normalized, palette, str(size), ident)
    if square:
        # drop the corner radius of the circular mask so the avatar fills its square frame
        svg = re.sub(r'(<mask[^>]*>\s*<rect[^>]*?) rx="[^"]*"', r'\1', svg, count=1)
    return svg
