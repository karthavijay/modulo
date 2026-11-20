"""Draws flat, on-brand course illustrations as SVG, from a small "scene" description.

Nothing here calls a network or an image service: every picture is built from reusable shapes
(people, desks, forklifts, racking, icons, checklists, charts) in the course colors, so it costs
nothing, looks consistent, and Modulo owns it outright. Adapted from the code-drawn image pack
used on TRIUMPH-2026-001.

A scene description (what Vera writes for each picture) is plain JSON:

  {"kind": "scene", "setting": "warehouse", "people": [{"position": "left", "pose": "clipboard",
   "outfit": "hi_vis"}], "props": [{"name": "forklift", "position": "right", "state": "lowered_load"}],
   "focus": "right", "badge": "warning", "alt": "..."}

Every field is checked against fixed lists, numbers are clamped, and no text from the description
ever ends up inside the SVG except the escaped alt text, so a description can't break or inject
anything. Pictures carry no words: labels stay as page text so they can be translated and read aloud.
"""

from __future__ import annotations

import html
import re
import zlib

# ---------------------------------------------------------------- palette

BASE = dict(blue="#2449A8", navy="#142033", coral="#F26B4F", mint="#2BB39A", sun="#FFC857", sky="#EEF3FB",
            slate="#4A5568", gray="#D5DCE6", paper="#FFFFFF", floor="#DCE5F2", night="#1B2A4A",
            vest="#F4D03F", wood="#C99A64", box="#D9A35F", leaf="#3F9A6B", grass="#CFE8D5")
SKIN = ["#8D5524", "#C68642", "#F1C27D", "#5C3A21", "#E0AC69"]
HAIR = ["#142033", "#2B1B10", "#6B4423", "#1A120B", "#3A2A1A", "#8A8F98"]
SHIRTS = ("blue", "coral", "mint", "navy", "slate", "sun")

W, H = 1600, 900
FLOOR = 720
POS = {"far_left": 200, "left": 450, "center_left": 640, "center": 800, "center_right": 960, "right": 1150, "far_right": 1400}
KINDS = ("scene", "icon", "icon_row", "flow", "checklist", "chart", "compare")
SETTINGS = ("office", "warehouse", "loading_dock", "home_night", "outdoors", "plain")
POSES = ("stand", "wave", "point", "phone", "clipboard", "walk", "sit")
OUTFITS = ("casual", "office", "hi_vis", "hi_vis_hard_hat", "visitor_vest")
PROPS = ("desk", "laptop_desk", "meeting_table", "whiteboard", "forklift", "pallet", "boxes", "racking",
         "cone", "warning_sign", "wall_clock", "plant", "tag", "fire_extinguisher", "first_aid_kit", "door")
STATES = ("normal", "alert", "raised_load", "lowered_load", "empty", "question", "chart", "checklist")
BADGES = ("none", "check", "cross", "warning", "question", "star")
TRENDS = ("up", "down", "flat")


def palette(accent: str | None = None) -> dict:
    c = dict(BASE)
    if accent and re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        c["blue"] = accent
    return c


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- icons (glyphs)
# Each glyph is drawn in a 120 x 120 box centred on 0,0, in one color.

def glyph(name: str, c: str) -> str:
    base = f'fill="none" stroke="{c}" stroke-linecap="round" stroke-linejoin="round"'
    s, s14, s8 = base + ' stroke-width="10"', base + ' stroke-width="14"', base + ' stroke-width="8"'
    g = {
        "check": f'<path d="M-36 2 L-10 28 L38 -26" {s14}/>',
        "cross": f'<path d="M-30 -30 L30 30 M30 -30 L-30 30" {s14}/>',
        "warning": f'<path d="M0 -46 L48 40 H-48Z" {s}/><path d="M0 -14 V12" {s}/><circle cx="0" cy="26" r="6" fill="{c}"/>',
        "clock": f'<circle r="44" {s}/><path d="M0 -24 V0 L18 14" {s}/>',
        "magnifier": f'<circle cx="-10" cy="-10" r="30" {s}/><path d="M12 12 L42 42" {s14}/>',
        "gear": "".join(f'<rect x="-8" y="-50" width="16" height="22" rx="4" fill="{c}" transform="rotate({a})"/>' for a in range(0, 360, 45))
                + f'<circle r="32" {s}/><circle r="10" fill="{c}"/>',
        "lightbulb": f'<path d="M-18 22 C-18 6 -34 0 -34 -18 A34 34 0 0 1 34 -18 C34 0 18 6 18 22Z" {s}/><path d="M-14 36 H14 M-8 48 H8" {s}/>',
        "question": f'<path d="M-24 -18 A24 24 0 1 1 8 4 C0 8 0 14 0 22" {s14}/><circle cx="0" cy="42" r="8" fill="{c}"/>',
        "chat": f'<path d="M-46 -34 H46 A10 10 0 0 1 56 -24 V18 A10 10 0 0 1 46 28 H-6 L-30 48 V28 H-46 A10 10 0 0 1 -56 18 V-24 A10 10 0 0 1 -46 -34Z" {s}/>'
                + "".join(f'<circle cx="{x}" cy="-3" r="6" fill="{c}"/>' for x in (-22, 0, 22)),
        "shield": f'<path d="M0 -48 L40 -32 V0 C40 26 20 42 0 50 C-20 42 -40 26 -40 0 V-32Z" {s}/><path d="M-16 0 L-4 12 L18 -12" {s}/>',
        "people": f'<circle cx="-20" cy="-22" r="14" {s}/><circle cx="22" cy="-22" r="14" {s}/><path d="M-46 36 C-46 6 6 6 6 36 M-4 8 C14 0 46 4 48 36" {s}/>',
        "clipboard": f'<rect x="-34" y="-42" width="68" height="90" rx="8" {s}/><rect x="-16" y="-50" width="32" height="16" rx="5" fill="{c}"/>'
                     + "".join(f'<path d="M-18 {y} H18" {s8}/>' for y in (-14, 4, 22)),
        "tag": f'<path d="M-44 -24 H20 L46 0 L20 24 H-44Z" {s}/><circle cx="18" cy="0" r="6" fill="{c}"/>',
        "chart": f'<path d="M-46 44 H48 M-46 44 V-46" {s}/>' + "".join(f'<rect x="{x}" y="{44 - h}" width="16" height="{h}" rx="3" fill="{c}"/>' for x, h in ((-30, 30), (-6, 54), (18, 76))),
        "star": f'<path d="M0 -48 L14 -16 L48 -14 L22 8 L30 42 L0 24 L-30 42 L-22 8 L-48 -14 L-14 -16Z" {s}/>',
        "flag": f'<path d="M-30 50 V-46 M-30 -42 H36 L22 -18 L36 6 H-30" {s}/>',
        "lock": f'<rect x="-36" y="-6" width="72" height="54" rx="10" {s}/><path d="M-22 -6 V-22 A22 22 0 0 1 22 -22 V-6" {s}/><circle cx="0" cy="20" r="7" fill="{c}"/>',
        "eye": f'<path d="M-54 0 C-30 -38 30 -38 54 0 C30 38 -30 38 -54 0Z" {s}/><circle r="16" fill="{c}"/>',
        "stop_hand": f'<path d="M-30 46 C-42 30 -44 12 -44 0 V-10 M-30 46 H22 C34 30 36 10 36 -6 V-28 M-24 0 V-40 M-4 0 V-50 M16 0 V-44 M36 -6 V-28" {s}/>',
        "book": f'<path d="M0 -30 C-18 -42 -38 -42 -52 -36 V40 C-38 34 -18 34 0 46 C18 34 38 34 52 40 V-36 C38 -42 18 -42 0 -30Z M0 -30 V46" {s}/>',
        "phone": f'<rect x="-26" y="-50" width="52" height="100" rx="10" {s}/><path d="M-8 34 H8" {s}/>',
        "forklift": f'<circle cx="-28" cy="34" r="12" {s}/><circle cx="18" cy="34" r="12" {s}/><path d="M-46 22 V-6 H8 V22 M-30 -6 V-40 H4 V-6 M30 -48 V40 H56" {s}/>',
        "box": f'<path d="M0 -46 L44 -24 V24 L0 46 L-44 24 V-24Z M-44 -24 L0 -2 L44 -24 M0 -2 V46" {s}/>',
        "calendar": f'<rect x="-46" y="-36" width="92" height="84" rx="10" {s}/><path d="M-46 -12 H46 M-22 -50 V-26 M22 -50 V-26" {s}/>'
                    + "".join(f'<circle cx="{x}" cy="{y}" r="5" fill="{c}"/>' for x in (-22, 0, 22) for y in (8, 28)),
        "target": f'<circle r="46" {s}/><circle r="26" {s}/><circle r="8" fill="{c}"/>',
        "heart": f'<path d="M0 44 C-40 16 -52 -4 -52 -20 A26 26 0 0 1 0 -30 A26 26 0 0 1 52 -20 C52 -4 40 16 0 44Z" {s}/>',
        "loop": f'<path d="M0 0 C14 -30 52 -30 52 0 C52 30 14 30 0 0 C-14 -30 -52 -30 -52 0 C-52 30 -14 30 0 0Z" {s}/>',
        "arrow_right": f'<path d="M-46 0 H40 M14 -26 L42 0 L14 26" {s14}/>',
        "document": f'<path d="M-34 -48 H16 L36 -28 V48 H-34Z M16 -48 V-28 H36" {s}/>' + "".join(f'<path d="M-18 {y} H20" {s8}/>' for y in (-6, 12, 30)),
        "fire": f'<path d="M0 48 C-30 48 -40 26 -36 8 C-32 -10 -14 -18 -10 -48 C14 -30 34 -8 36 14 C38 34 24 48 0 48Z" {s}/>',
        "first_aid": f'<rect x="-46" y="-40" width="92" height="80" rx="14" {s}/><path d="M0 -20 V20 M-20 0 H20" {s14}/>',
    }
    return g.get(name, g["star"])


GLYPHS = ("check", "cross", "warning", "clock", "magnifier", "gear", "lightbulb", "question", "chat", "shield", "people",
          "clipboard", "tag", "chart", "star", "flag", "lock", "eye", "stop_hand", "book", "phone", "forklift", "box",
          "calendar", "target", "heart", "loop", "arrow_right", "document", "fire", "first_aid")


def badge(kind: str, x: float, y: float, C: dict, r: float = 52) -> str:
    if kind not in BADGES or kind == "none":
        return ""
    bg = {"check": C["mint"], "cross": C["coral"], "warning": C["sun"], "question": C["blue"], "star": C["sun"]}[kind]
    fg = C["navy"] if kind in ("warning", "star") else C["paper"]
    k = r / 60
    return (f'<g transform="translate({_f(x)},{_f(y)})"><circle r="{_f(r + 6)}" fill="{C["paper"]}"/><circle r="{_f(r)}" fill="{bg}"/>'
            f'<g transform="scale({_f(k * 0.82)})">{glyph(kind, fg)}</g></g>')


# ---------------------------------------------------------------- people

def person(x: float, feet: float, s: float, C: dict, skin: str, hair: str, shirt: str,
           pose: str = "stand", outfit: str = "casual", flip: bool = False) -> str:
    """A person whose feet (or seat, when sitting) are at (x, feet). Unscaled height is about 230."""
    seated = pose == "sit"
    oy = feet - (110 if seated else 160) * s
    sx = -s if flip else s
    g = f'<g transform="translate({_f(x)},{_f(oy)}) scale({_f(sx)},{_f(s)})">'
    if seated:
        g += f'<rect x="-30" y="66" width="60" height="44" rx="14" fill="{C["navy"]}"/>'
    elif pose == "walk":
        g += (f'<rect x="-9" y="70" width="18" height="92" rx="8" fill="{C["navy"]}" transform="rotate(14 0 76)"/>'
              f'<rect x="-9" y="70" width="18" height="92" rx="8" fill="{C["navy"]}" transform="rotate(-14 0 76)"/>')
    else:
        g += f'<rect x="-22" y="70" width="18" height="90" rx="8" fill="{C["navy"]}"/><rect x="4" y="70" width="18" height="90" rx="8" fill="{C["navy"]}"/>'
        g += f'<rect x="-26" y="152" width="24" height="10" rx="5" fill="{C["navy"]}"/><rect x="2" y="152" width="24" height="10" rx="5" fill="{C["navy"]}"/>'
    g += f'<rect x="-34" y="-6" width="68" height="92" rx="26" fill="{shirt}"/>'
    if outfit in ("hi_vis", "hi_vis_hard_hat", "visitor_vest"):
        vest = C["vest"] if outfit != "visitor_vest" else "#F59A3C"
        g += (f'<path d="M-32 8 Q-32 -4 -20 -4 L-6 -4 L0 22 L6 -4 L20 -4 Q32 -4 32 8 L32 70 Q32 84 18 84 L-18 84 Q-32 84 -32 70Z" fill="{vest}"/>'
              f'<rect x="-32" y="40" width="64" height="7" fill="#E9EEF4"/><rect x="-32" y="58" width="64" height="7" fill="#E9EEF4"/>')
    elif outfit == "office":
        g += f'<path d="M-8 -6 L0 26 L8 -6Z" fill="{C["paper"]}"/><path d="M-3 0 L0 30 L3 0Z" fill="{C["navy"]}"/>'
    if pose == "wave":
        g += f'<path d="M28 10 L58 -40" stroke="{shirt}" stroke-width="16" stroke-linecap="round"/><circle cx="60" cy="-44" r="10" fill="{skin}"/>'
    elif pose == "point":
        g += f'<path d="M28 14 L80 0" stroke="{shirt}" stroke-width="16" stroke-linecap="round"/><circle cx="84" cy="0" r="9" fill="{skin}"/>'
    elif pose == "phone":
        g += (f'<path d="M24 20 L40 -14" stroke="{shirt}" stroke-width="16" stroke-linecap="round"/><rect x="30" y="-46" width="26" height="40" rx="5" fill="{C["navy"]}"/>'
              f'<rect x="34" y="-41" width="18" height="28" rx="2" fill="{C["blue"]}"/><circle cx="40" cy="-8" r="9" fill="{skin}"/>')
    elif pose == "clipboard":
        g += (f'<path d="M26 22 L52 38" stroke="{shirt}" stroke-width="16" stroke-linecap="round"/><rect x="40" y="0" width="44" height="58" rx="5" fill="{C["wood"]}"/>'
              f'<rect x="45" y="6" width="34" height="46" rx="2" fill="{C["paper"]}"/>'
              + "".join(f'<rect x="50" y="{14 + i * 11}" width="{22 - i * 3}" height="5" rx="2" fill="{C["blue"]}" opacity=".6"/>' for i in range(3))
              + f'<circle cx="56" cy="40" r="9" fill="{skin}"/>')
    elif pose == "sit":
        g += f'<path d="M26 24 L64 38" stroke="{shirt}" stroke-width="16" stroke-linecap="round"/><circle cx="68" cy="40" r="9" fill="{skin}"/>'
    g += f'<rect x="-9" y="-20" width="18" height="18" fill="{skin}"/><circle cx="0" cy="-40" r="28" fill="{skin}"/>'
    if outfit == "hi_vis_hard_hat":
        g += f'<path d="M-30 -48 A30 30 0 0 1 30 -48Z" fill="{C["sun"]}"/><rect x="-36" y="-52" width="72" height="9" rx="4" fill="{C["sun"]}"/><rect x="-4" y="-78" width="8" height="26" rx="3" fill="#F0B73E"/>'
    else:
        g += f'<path d="M-29 -42 Q-28 -74 0 -72 Q28 -74 29 -42 Q18 -58 -29 -42Z" fill="{hair}"/>'
    return g + "</g>"


# ---------------------------------------------------------------- props

def desk(x: float, base: float, C: dict, s: float = 1.4, alert: bool = False, laptop: bool = False) -> str:
    w = 260
    g = f'<g transform="translate({_f(x - w * s / 2)},{_f(base - 124 * s)}) scale({_f(s)})">'
    g += f'<rect x="0" y="0" width="{w}" height="14" rx="4" fill="{C["slate"]}"/><rect x="18" y="14" width="12" height="110" fill="{C["slate"]}"/><rect x="{w - 30}" y="14" width="12" height="110" fill="{C["slate"]}"/>'
    if laptop:
        g += f'<path d="M70 0 L90 -70 H180 L170 0Z" fill="{C["navy"]}"/><path d="M95 -62 H172 L164 -8 H82Z" fill="{C["coral"] if alert else C["sky"]}" opacity="{1 if not alert else .25}"/><rect x="60" y="-6" width="130" height="8" rx="3" fill="{C["gray"]}"/>'
        if alert:
            g += f'<path d="M126 -56 L146 -20 L106 -20Z" fill="{C["coral"]}"/><rect x="124" y="-48" width="4" height="14" rx="2" fill="{C["navy"]}"/>'
    else:
        g += f'<rect x="{w / 2 - 70}" y="-110" width="140" height="92" rx="8" fill="{C["navy"]}"/><rect x="{w / 2 - 62}" y="-102" width="124" height="76" rx="4" fill="{C["sky"]}"/><rect x="{w / 2 - 8}" y="-18" width="16" height="18" fill="{C["navy"]}"/>'
        if alert:
            g += f'<path d="M{w / 2} -94 L{w / 2 + 26} -48 L{w / 2 - 26} -48Z" fill="{C["coral"]}"/><rect x="{w / 2 - 3}" y="-84" width="6" height="20" rx="3" fill="{C["navy"]}"/><circle cx="{w / 2}" cy="-56" r="4" fill="{C["navy"]}"/>'
        else:
            g += "".join(f'<rect x="{w / 2 - 50}" y="{-92 + i * 16}" width="{100 - i * 18}" height="8" rx="4" fill="{C["blue"]}" opacity=".55"/>' for i in range(4))
    return g + "</g>"


def meeting_table(x: float, base: float, C: dict) -> str:
    return (f'<rect x="{_f(x - 260)}" y="{_f(base - 150)}" width="520" height="22" rx="10" fill="{C["slate"]}"/>'
            f'<rect x="{_f(x - 220)}" y="{_f(base - 128)}" width="16" height="128" fill="{C["slate"]}"/><rect x="{_f(x + 204)}" y="{_f(base - 128)}" width="16" height="128" fill="{C["slate"]}"/>'
            f'<rect x="{_f(x - 150)}" y="{_f(base - 172)}" width="90" height="22" rx="4" fill="{C["paper"]}"/><rect x="{_f(x + 40)}" y="{_f(base - 176)}" width="40" height="26" rx="6" fill="{C["coral"]}"/>')


def whiteboard(x: float, base: float, C: dict, content: str = "normal") -> str:
    left, top, w, h = x - 300, base - 560, 600, 380
    g = (f'<rect x="{_f(left + 70)}" y="{_f(top + h)}" width="14" height="{_f(base - top - h)}" fill="{C["slate"]}"/>'
         f'<rect x="{_f(left + w - 84)}" y="{_f(top + h)}" width="14" height="{_f(base - top - h)}" fill="{C["slate"]}"/>'
         f'<rect x="{_f(left)}" y="{_f(top)}" width="{w}" height="{h}" rx="16" fill="{C["paper"]}" stroke="{C["slate"]}" stroke-width="10"/>')
    cx, cy = x, top + h / 2
    if content == "question":
        g += f'<g transform="translate({_f(cx)},{_f(cy)}) scale(2)">{glyph("question", C["blue"])}</g>'
    elif content == "chart":
        g += "".join(f'<rect x="{_f(left + 90 + i * 100)}" y="{_f(top + h - 50 - hh)}" width="60" height="{hh}" rx="6" fill="{C["blue"]}"/>' for i, hh in enumerate((90, 140, 120, 220)))
        g += f'<path d="M{_f(left + 80)} {_f(top + 230)} L{_f(left + 220)} {_f(top + 170)} L{_f(left + 330)} {_f(top + 190)} L{_f(left + 500)} {_f(top + 70)}" fill="none" stroke="{C["coral"]}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>'
    elif content == "checklist":
        for i in range(4):
            y = top + 60 + i * 74
            g += f'<rect x="{_f(left + 70)}" y="{_f(y)}" width="40" height="40" rx="8" fill="{C["paper"]}" stroke="{C["blue"]}" stroke-width="5"/>'
            g += f'<path d="M{_f(left + 78)} {_f(y + 20)} l10 10 16 -20" fill="none" stroke="{C["mint"]}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
            g += f'<rect x="{_f(left + 136)}" y="{_f(y + 13)}" width="{[340, 280, 320, 240][i]}" height="14" rx="7" fill="{C["navy"]}" opacity=".5"/>'
    elif content == "alert":
        g += f'<g transform="translate({_f(cx)},{_f(cy)}) scale(2.2)">{glyph("warning", C["coral"])}</g>'
    elif content != "empty":
        g += "".join(f'<rect x="{_f(left + 70)}" y="{_f(top + 70 + i * 60)}" width="{[420, 360, 400, 300, 200][i]}" height="16" rx="8" fill="{C["blue"]}" opacity=".45"/>' for i in range(5))
    return g


def forklift(x: float, base: float, C: dict, s: float = 1.15, flip: bool = False, load: str = "lowered_load",
             operator: dict | None = None, alert: bool = False) -> str:
    """Counterbalance forklift, side view, forks to the right (left when flip). x is the body centre."""
    sx = -s if flip else s
    g = f'<g transform="translate({_f(x)},{_f(base)}) scale({_f(sx)},{_f(s)})"><g transform="translate(-200,0)">'
    fork_y = -250 if load == "raised_load" else -26
    # mast behind the load
    g += f'<rect x="300" y="-390" width="18" height="372" rx="4" fill="{C["slate"]}"/><rect x="326" y="-370" width="12" height="352" rx="4" fill="{C["slate"]}"/>'
    # overhead guard
    g += (f'<rect x="108" y="-330" width="12" height="190" fill="{C["navy"]}"/><rect x="250" y="-330" width="12" height="200" fill="{C["navy"]}"/>'
          f'<rect x="96" y="-342" width="180" height="16" rx="6" fill="{C["navy"]}"/>')
    if operator:
        g += person(170, -150, 0.88, C, operator["skin"], operator["hair"], operator["shirt"], "sit", operator.get("outfit", "hi_vis"))
    # body and counterweight
    g += (f'<path d="M30 -170 H240 Q290 -170 296 -120 L300 -52 H20Z" fill="{C["sun"]}"/>'
          f'<rect x="0" y="-188" width="78" height="150" rx="26" fill="{C["navy"]}"/>'
          f'<rect x="130" y="-180" width="70" height="14" rx="6" fill="{C["slate"]}"/>'
          f'<rect x="20" y="-62" width="290" height="22" rx="8" fill="{C["slate"]}"/>')
    # wheels
    for wx, r in ((80, 38), (250, 42)):
        g += f'<circle cx="{wx}" cy="-{r}" r="{r}" fill="{C["navy"]}"/><circle cx="{wx}" cy="-{r}" r="{r * 0.42:.0f}" fill="{C["gray"]}"/>'
    # carriage, forks and load
    g += f'<rect x="332" y="{fork_y - 78}" width="14" height="88" rx="3" fill="{C["navy"]}"/><rect x="332" y="{fork_y}" width="150" height="12" rx="3" fill="{C["navy"]}"/>'
    if load in ("raised_load", "lowered_load"):
        py = fork_y - 16
        g += f'<rect x="346" y="{py}" width="132" height="16" rx="2" fill="{C["wood"]}"/>'
        for i, (bx, by, bw, bh) in enumerate(((350, -60, 60, 60), (414, -60, 60, 60), (370, -116, 70, 56))):
            g += f'<rect x="{bx}" y="{py + by}" width="{bw}" height="{bh}" rx="4" fill="{C["box"]}" stroke="#B9843F" stroke-width="3"/><path d="M{bx + bw / 2} {py + by} v18" stroke="#B9843F" stroke-width="5"/>'
    if alert:
        g += f'<g transform="translate(185,-400)">{badge("warning", 0, 0, C, 40)}</g>'
    return g + "</g></g>"


def pallet(x: float, base: float, C: dict, boxes: bool = True) -> str:
    g = f'<rect x="{_f(x - 90)}" y="{_f(base - 24)}" width="180" height="24" rx="3" fill="{C["wood"]}"/>'
    g += "".join(f'<rect x="{_f(x - 84 + i * 66)}" y="{_f(base - 12)}" width="36" height="12" fill="#A97C4C"/>' for i in range(3))
    if boxes:
        for bx, by, bw, bh in ((-86, -104, 84, 80), (2, -104, 84, 80), (-56, -180, 100, 76)):
            g += f'<rect x="{_f(x + bx)}" y="{_f(base + by)}" width="{bw}" height="{bh}" rx="4" fill="{C["box"]}" stroke="#B9843F" stroke-width="3"/><path d="M{_f(x + bx + bw / 2)} {_f(base + by)} v24" stroke="#B9843F" stroke-width="6"/>'
    return g


def racking(x: float, base: float, C: dict, bays: int = 2) -> str:
    bw, lv = 260, (0, 170, 340)
    left = x - bays * bw / 2
    g = ""
    for b in range(bays):
        bx = left + b * bw
        for li, ly in enumerate(lv):
            y = base - 30 - ly
            if (b + li) % 3 != 2:
                for k in range(2):
                    g += f'<rect x="{_f(bx + 22 + k * 112)}" y="{_f(y - 96)}" width="100" height="92" rx="4" fill="{C["box"]}" stroke="#B9843F" stroke-width="3"/>'
            g += f'<rect x="{_f(bx)}" y="{_f(y)}" width="{bw}" height="14" fill="{C["coral"]}"/>'
    for b in range(bays + 1):
        g += f'<rect x="{_f(left + b * bw - 8)}" y="{_f(base - 520)}" width="16" height="520" fill="{C["blue"]}"/>'
    return g


def cone(x: float, base: float, C: dict) -> str:
    return (f'<path d="M{_f(x - 26)} {_f(base - 12)} L{_f(x - 8)} {_f(base - 120)} H{_f(x + 8)} L{_f(x + 26)} {_f(base - 12)}Z" fill="#F28C38"/>'
            f'<path d="M{_f(x - 18)} {_f(base - 58)} H{_f(x + 18)} L{_f(x + 14)} {_f(base - 80)} H{_f(x - 14)}Z" fill="{C["paper"]}"/>'
            f'<rect x="{_f(x - 40)}" y="{_f(base - 14)}" width="80" height="14" rx="4" fill="#E0762A"/>')


def warning_sign(x: float, base: float, C: dict) -> str:
    return (f'<path d="M{_f(x - 70)} {_f(base)} L{_f(x - 40)} {_f(base - 230)} H{_f(x + 40)} L{_f(x + 70)} {_f(base)}" fill="none" stroke="{C["slate"]}" stroke-width="12" stroke-linejoin="round"/>'
            f'<path d="M{_f(x)} {_f(base - 210)} L{_f(x + 60)} {_f(base - 100)} H{_f(x - 60)}Z" fill="{C["sun"]}" stroke="{C["navy"]}" stroke-width="8" stroke-linejoin="round"/>'
            f'<rect x="{_f(x - 5)}" y="{_f(base - 180)}" width="10" height="44" rx="5" fill="{C["navy"]}"/><circle cx="{_f(x)}" cy="{_f(base - 120)}" r="7" fill="{C["navy"]}"/>')


def wall_clock(x: float, C: dict) -> str:
    return f'<g transform="translate({_f(x)},170)"><circle r="56" fill="{C["paper"]}" stroke="{C["navy"]}" stroke-width="8"/>{glyph("clock", C["navy"]).replace("r=\"44\"", "r=\"0\"")}</g>'


def plant(x: float, base: float, C: dict) -> str:
    return (f'<path d="M{_f(x)} {_f(base - 90)} C{_f(x - 70)} {_f(base - 150)} {_f(x - 60)} {_f(base - 230)} {_f(x - 10)} {_f(base - 250)} C{_f(x + 10)} {_f(base - 190)} {_f(x + 6)} {_f(base - 130)} {_f(x)} {_f(base - 90)}Z" fill="{C["leaf"]}"/>'
            f'<path d="M{_f(x)} {_f(base - 90)} C{_f(x + 60)} {_f(base - 130)} {_f(x + 80)} {_f(base - 190)} {_f(x + 50)} {_f(base - 220)} C{_f(x + 20)} {_f(base - 170)} {_f(x + 6)} {_f(base - 130)} {_f(x)} {_f(base - 90)}Z" fill="{C["mint"]}"/>'
            f'<path d="M{_f(x - 40)} {_f(base - 96)} H{_f(x + 40)} L{_f(x + 30)} {_f(base)} H{_f(x - 30)}Z" fill="{C["coral"]}"/>')


def tag(x: float, base: float, C: dict) -> str:
    y = base - 330
    return (f'<g transform="rotate(-6 {_f(x)} {_f(y)})"><rect x="{_f(x - 80)}" y="{_f(y)}" width="160" height="250" rx="16" fill="{C["coral"]}"/>'
            f'<circle cx="{_f(x)}" cy="{_f(y + 30)}" r="14" fill="{C["paper"]}"/><path d="M{_f(x)} {_f(y + 16)} L{_f(x - 40)} {_f(y - 60)}" stroke="{C["slate"]}" stroke-width="5"/>'
            f'<g transform="translate({_f(x)},{_f(y + 120)}) scale(1.1)">{glyph("stop_hand", C["paper"])}</g>'
            f'<rect x="{_f(x - 50)}" y="{_f(y + 196)}" width="100" height="12" rx="6" fill="{C["paper"]}" opacity=".8"/><rect x="{_f(x - 34)}" y="{_f(y + 218)}" width="68" height="12" rx="6" fill="{C["paper"]}" opacity=".8"/></g>')


def fire_extinguisher(x: float, base: float, C: dict) -> str:
    return (f'<rect x="{_f(x - 28)}" y="{_f(base - 200)}" width="56" height="200" rx="24" fill="#D9453B"/>'
            f'<rect x="{_f(x - 12)}" y="{_f(base - 236)}" width="24" height="40" rx="4" fill="{C["navy"]}"/><path d="M{_f(x + 10)} {_f(base - 226)} C{_f(x + 60)} {_f(base - 226)} {_f(x + 60)} {_f(base - 150)} {_f(x + 40)} {_f(base - 110)}" fill="none" stroke="{C["navy"]}" stroke-width="10" stroke-linecap="round"/>'
            f'<rect x="{_f(x - 28)}" y="{_f(base - 150)}" width="56" height="40" fill="{C["paper"]}" opacity=".85"/>')


def first_aid_kit(x: float, base: float, C: dict) -> str:
    return (f'<rect x="{_f(x - 34)}" y="{_f(base - 156)}" width="68" height="20" rx="8" fill="none" stroke="{C["slate"]}" stroke-width="10"/>'
            f'<rect x="{_f(x - 90)}" y="{_f(base - 140)}" width="180" height="140" rx="18" fill="{C["mint"]}"/>'
            f'<path d="M{_f(x)} {_f(base - 106)} V{_f(base - 34)} M{_f(x - 36)} {_f(base - 70)} H{_f(x + 36)}" stroke="{C["paper"]}" stroke-width="22" stroke-linecap="round"/>')


def door(x: float, base: float, C: dict) -> str:
    return (f'<rect x="{_f(x - 90)}" y="{_f(base - 420)}" width="180" height="420" rx="6" fill="{C["slate"]}"/>'
            f'<rect x="{_f(x - 76)}" y="{_f(base - 406)}" width="152" height="406" rx="4" fill="{C["blue"]}"/>'
            f'<rect x="{_f(x - 56)}" y="{_f(base - 380)}" width="112" height="120" rx="6" fill="{C["sky"]}"/><circle cx="{_f(x + 52)}" cy="{_f(base - 200)}" r="9" fill="{C["sun"]}"/>')


# ---------------------------------------------------------------- settings (backgrounds)

def setting(name: str, C: dict) -> str:
    if name == "home_night":
        g = f'<rect width="{W}" height="{H}" fill="{C["night"]}"/><rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="#2A3A5E"/>'
        g += f'<rect x="1080" y="110" width="380" height="280" rx="14" fill="#2F4A7A" stroke="{C["gray"]}" stroke-width="10"/><path d="M1270 110 V390 M1080 250 H1460" stroke="{C["gray"]}" stroke-width="8"/>'
        g += f'<circle cx="1380" cy="180" r="38" fill="{C["sun"]}"/><circle cx="1398" cy="166" r="34" fill="#2F4A7A"/>'
        g += "".join(f'<circle cx="{x}" cy="{y}" r="4" fill="{C["paper"]}"/>' for x, y in ((1130, 160), (1200, 210), (1330, 320), (1420, 350), (1150, 340)))
        return g
    if name == "outdoors":
        g = f'<rect width="{W}" height="{H}" fill="#E4EEFB"/><circle cx="1420" cy="140" r="56" fill="{C["sun"]}"/>'
        g += f'<path d="M0 {FLOOR - 60} C300 {FLOOR - 140} 600 {FLOOR - 40} 900 {FLOOR - 100} C1200 {FLOOR - 160} 1400 {FLOOR - 70} 1600 {FLOOR - 110} V{H} H0Z" fill="{C["grass"]}"/>'
        g += f'<rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="#C9D3DF"/>'
        g += "".join(f'<rect x="{x}" y="{FLOOR + 80}" width="90" height="12" rx="6" fill="{C["paper"]}" opacity=".8"/>' for x in range(40, W, 200))
        for tx in (120, 1520):
            g += f'<rect x="{tx - 10}" y="{FLOOR - 170}" width="20" height="120" fill="#7A5A3A"/><circle cx="{tx}" cy="{FLOOR - 200}" r="70" fill="{C["leaf"]}"/>'
        return g
    g = f'<rect width="{W}" height="{H}" fill="{C["sky"]}"/>'
    if name == "plain":
        return g + f'<circle cx="1460" cy="130" r="40" fill="{C["sun"]}" opacity=".7"/><circle cx="150" cy="160" r="22" fill="{C["mint"]}" opacity=".5"/><rect y="{FLOOR + 40}" width="{W}" height="{H - FLOOR - 40}" fill="{C["floor"]}"/>'
    g += f'<rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="{C["floor"]}"/>'
    if name == "warehouse":
        g = f'<rect width="{W}" height="{H}" fill="#E6ECF4"/><rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="#D3DCE8"/>'
        g += "".join(f'<rect x="{x}" y="40" width="120" height="26" rx="6" fill="{C["paper"]}" opacity=".9"/>' for x in (240, 740, 1240))
        g += f'<g opacity=".35">{racking(260, FLOOR, C, 2)}{racking(1340, FLOOR, C, 2)}</g>'
        g += f'<rect y="{FLOOR + 60}" width="{W}" height="12" fill="{C["sun"]}"/><rect y="{FLOOR + 150}" width="{W}" height="12" fill="{C["sun"]}"/>'
        return g
    if name == "loading_dock":
        g = f'<rect width="{W}" height="{H}" fill="#E6ECF4"/><rect y="{FLOOR}" width="{W}" height="{H - FLOOR}" fill="#D3DCE8"/>'
        for i, dx in enumerate((110, 610, 1110)):
            g += f'<rect x="{dx}" y="200" width="380" height="{FLOOR - 200}" rx="6" fill="{C["slate"]}"/>'
            if i == 1:
                g += f'<rect x="{dx + 16}" y="216" width="348" height="{FLOOR - 216}" fill="#2B3445"/><rect x="{dx + 30}" y="{FLOOR - 120}" width="80" height="104" fill="{C["box"]}" opacity=".5"/><rect x="{dx + 120}" y="{FLOOR - 90}" width="70" height="74" fill="{C["box"]}" opacity=".45"/>'
                g += f'<rect x="{dx + 16}" y="216" width="348" height="70" fill="{C["gray"]}"/>' + "".join(f'<rect x="{dx + 16}" y="{226 + k * 18}" width="348" height="4" fill="{C["slate"]}" opacity=".4"/>' for k in range(3))
            else:
                g += f'<rect x="{dx + 16}" y="216" width="348" height="{FLOOR - 216}" fill="{C["gray"]}"/>' + "".join(f'<rect x="{dx + 16}" y="{236 + k * 40}" width="348" height="6" fill="{C["slate"]}" opacity=".35"/>' for k in range(12))
            g += f'<rect x="{dx}" y="{FLOOR - 14}" width="380" height="14" fill="{C["sun"]}"/>'
        g += f'<rect y="{FLOOR}" width="{W}" height="16" fill="{C["sun"]}" opacity=".9"/>'
        return g
    # office
    g += f'<rect x="1100" y="110" width="360" height="250" rx="12" fill="{C["paper"]}" stroke="{C["gray"]}" stroke-width="10"/><path d="M1280 110 V360" stroke="{C["gray"]}" stroke-width="8"/>'
    g += f'<path d="M1110 330 L1200 250 L1270 320 L1350 230 L1450 330Z" fill="{C["mint"]}" opacity=".35"/>'
    g += f'<rect x="150" y="150" width="220" height="150" rx="8" fill="{C["paper"]}" stroke="{C["gray"]}" stroke-width="8"/><circle cx="230" cy="210" r="30" fill="{C["sun"]}" opacity=".8"/><rect x="190" y="256" width="140" height="14" rx="7" fill="{C["blue"]}" opacity=".4"/>'
    return g


# ---------------------------------------------------------------- the scene builder

def _enum(v, allowed, default):
    return v if isinstance(v, str) and v in allowed else default


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _people(spec_people, C: dict) -> list[dict]:
    out = []
    for i, p in enumerate((spec_people or [])[:4]):
        if not isinstance(p, dict):
            continue
        skin = _int(p.get("skin"), 1, 5, (i * 2) % 5 + 1)
        out.append({
            "position": _enum(p.get("position"), POS, list(POS)[(i * 2 + 1) % len(POS)]),
            "pose": _enum(p.get("pose"), POSES, "stand"),
            "outfit": _enum(p.get("outfit"), OUTFITS, "casual"),
            "shirt": C[_enum(p.get("shirt"), SHIRTS, SHIRTS[i % 3])],
            "skin": SKIN[skin - 1],
            "hair": HAIR[(i * 3 + skin) % len(HAIR)],
            "facing": _enum(p.get("facing"), ("left", "right"), ""),
        })
    return out


def _props(spec_props) -> list[dict]:
    out = []
    for p in (spec_props or [])[:5]:
        if isinstance(p, dict) and p.get("name") in PROPS:
            out.append({"name": p["name"], "position": _enum(p.get("position"), POS, "center"),
                        "state": _enum(p.get("state"), STATES, "normal")})
    return out


BACK = {"whiteboard", "racking", "wall_clock", "door", "tag"}


def _scene_body(spec: dict, C: dict) -> str:
    """Setting, props and people for a 1600 x 900 scene."""
    body = setting(_enum(spec.get("setting"), SETTINGS, "office"), C)
    people = _people(spec.get("people"), C)
    props = _props(spec.get("props"))
    base = FLOOR + 70
    seated_at: dict[str, dict] = {}
    for p in people:
        if p["pose"] == "sit":
            for pr in props:
                if pr["name"] == "forklift" and pr["position"] == p["position"] and pr["position"] not in seated_at:
                    seated_at[pr["position"]] = p
    drawn = set()

    def draw_prop(pr) -> str:
        x, name, st = POS[pr["position"]], pr["name"], pr["state"]
        if name == "desk":
            return desk(x, base, C, alert=st == "alert")
        if name == "laptop_desk":
            return desk(x, base, C, alert=st == "alert", laptop=True)
        if name == "meeting_table":
            return meeting_table(x, base, C)
        if name == "whiteboard":
            return whiteboard(x, base, C, st)
        if name == "forklift":
            op = seated_at.get(pr["position"])
            if op:
                drawn.add(id(op))
            flip = x > W / 2 if not op or not op["facing"] else op["facing"] == "left"
            load = st if st in ("raised_load", "lowered_load", "empty") else "lowered_load"
            return forklift(x, base, C, flip=flip, load=load, operator=op, alert=st == "alert")
        if name == "pallet":
            return pallet(x, base, C, boxes=st != "empty")
        if name == "boxes":
            return pallet(x, base, C) + pallet(x + 200, base, C, boxes=True).replace(C["wood"], C["wood"])
        if name == "racking":
            return racking(x, base, C, 1)
        if name == "cone":
            return cone(x - 40, base, C) + cone(x + 60, base + 10, C)
        if name == "warning_sign":
            return warning_sign(x, base, C)
        if name == "wall_clock":
            return wall_clock(x, C)
        if name == "plant":
            return plant(x, base, C)
        if name == "tag":
            return tag(x, base, C)
        if name == "fire_extinguisher":
            return fire_extinguisher(x, base, C)
        if name == "first_aid_kit":
            return first_aid_kit(x, base - 300, C) + f'<rect x="{_f(x - 110)}" y="{_f(base - 300)}" width="220" height="12" rx="4" fill="{C["slate"]}"/>'
        if name == "door":
            return door(x, base, C)
        return ""

    back = [p for p in props if p["name"] in BACK]
    front = [p for p in props if p["name"] not in BACK]
    body += "".join(draw_prop(p) for p in back)
    desks = {p["position"] for p in front if p["name"] in ("desk", "laptop_desk", "meeting_table")}
    # seated people at desks sit behind them; everyone else stands in front of the props
    for p in people:
        if p["pose"] == "sit" and p["position"] in desks:
            x = POS[p["position"]]
            body += person(x + 90, base - 150, 1.6, C, p["skin"], p["hair"], p["shirt"], "sit", p["outfit"], flip=p["facing"] != "right")
            drawn.add(id(p))
    body += "".join(draw_prop(p) for p in front)
    for p in people:
        if id(p) in drawn:
            continue
        x = POS[p["position"]]
        flip = (p["facing"] == "left") if p["facing"] else x > W / 2
        if p["pose"] == "sit":  # nowhere to sit: draw a simple chair
            body += f'<rect x="{_f(x - 50)}" y="{_f(base - 120)}" width="100" height="16" rx="6" fill="{C["slate"]}"/><rect x="{_f(x - 6)}" y="{_f(base - 104)}" width="12" height="104" fill="{C["slate"]}"/>'
            body += person(x, base - 120, 1.6, C, p["skin"], p["hair"], p["shirt"], "sit", p["outfit"], flip)
        else:
            body += person(x, base + 20, 1.75, C, p["skin"], p["hair"], p["shirt"], p["pose"], p["outfit"], flip)
    focus = _enum(spec.get("focus"), POS, "")
    if focus:
        fx = POS[focus]
        body += f'<ellipse cx="{fx}" cy="{FLOOR - 170}" rx="210" ry="330" fill="none" stroke="{C["coral"]}" stroke-width="10" stroke-dasharray="26 18" stroke-linecap="round"/>'
    body += badge(_enum(spec.get("badge"), BADGES, "none"), W - 120, 120, C)
    return body


def _icon_tile(name: str, x: float, y: float, size: float, C: dict, color: str | None = None, bg: str | None = None) -> str:
    color = color or C["blue"]
    return (f'<g transform="translate({_f(x)},{_f(y)})"><rect x="{_f(-size / 2)}" y="{_f(-size / 2)}" width="{_f(size)}" height="{_f(size)}" rx="{_f(size * 0.2)}" fill="{bg or C["paper"]}"/>'
            f'<g transform="scale({_f(size / 170)})">{glyph(name, color)}</g></g>')


def _glyphs(spec: dict, n_max: int = 6) -> list[str]:
    return [g for g in (spec.get("glyphs") or []) if g in GLYPHS][:n_max]


def draw(spec: dict, accent: str | None = None) -> tuple[str, int, int]:
    """Return (svg, width, height) for one scene description."""
    C = palette(accent)
    spec = spec if isinstance(spec, dict) else {}
    kind = _enum(spec.get("kind"), KINDS, "scene")
    w, h = W, H
    if kind == "icon":
        w = h = 600
        g = (_glyphs(spec) or ["star"])[0]
        col = C["coral"] if g in ("warning", "cross", "fire", "stop_hand") else C["mint"] if g in ("check", "shield", "first_aid") else C["blue"]
        body = f'<rect width="600" height="600" rx="120" fill="{C["sky"]}"/><circle cx="300" cy="300" r="200" fill="{C["paper"]}"/><g transform="translate(300,300) scale(2.6)">{glyph(g, col)}</g>'
        b = _enum(spec.get("badge"), BADGES, "none")
        body += badge(b, 470, 130, C, 56) if b != g else ""
    elif kind == "icon_row":
        gs = _glyphs(spec) or ["star", "check"]
        n = len(gs)
        w, h = 1600, 520
        size = min(300, (1600 - 80 * (n + 1)) / n)
        gap = (1600 - n * size) / (n + 1)
        cols = [C["blue"], C["mint"], C["coral"], C["navy"], C["blue"], C["mint"]]
        body = f'<rect width="{w}" height="{h}" fill="{C["sky"]}"/>'
        body += "".join(_icon_tile(g, gap + size / 2 + i * (size + gap), h / 2, size, C, cols[i]) for i, g in enumerate(gs))
    elif kind == "flow":
        gs = _glyphs(spec) or ["clipboard", "gear", "check"]
        n = max(2, len(gs))
        gs = (gs + ["arrow_right"] * n)[:n]
        w, h = 1600, 640
        size = min(240, (1600 - 70 * (n + 1)) / n)
        gap = (1600 - n * size) / (n + 1)
        hi = _int(spec.get("highlight"), -1, n - 1, -1)
        lb = _int(spec.get("loop_back_to"), -1, n - 2, -1)
        cy = 280
        body = f'<rect width="{w}" height="{h}" fill="{C["sky"]}"/>'
        xs = [gap + size / 2 + i * (size + gap) for i in range(n)]
        for i, x in enumerate(xs):
            if i < n - 1:
                a, b = x + size / 2 + 14, xs[i + 1] - size / 2 - 14
                body += f'<path d="M{_f(a)} {cy} H{_f(b - 6)}" stroke="{C["navy"]}" stroke-width="10" stroke-linecap="round"/><path d="M{_f(b - 22)} {cy - 16} L{_f(b)} {cy} L{_f(b - 22)} {cy + 16}" fill="none" stroke="{C["navy"]}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>'
            ring = f' stroke="{C["coral"]}" stroke-width="12"' if i == hi else f' stroke="{C["blue"]}" stroke-width="6"'
            body += f'<rect x="{_f(x - size / 2)}" y="{_f(cy - size / 2)}" width="{_f(size)}" height="{_f(size)}" rx="{_f(size * 0.18)}" fill="{C["paper"]}"{ring}/>'
            body += f'<g transform="translate({_f(x)},{cy}) scale({_f(size / 160)})">{glyph(gs[i], C["coral"] if i == hi else C["blue"])}</g>'
        if hi >= 0:
            body += "".join(f'<rect x="{_f(xs[hi] - 70 + (k % 3) * 46)}" y="{_f(cy + size / 2 + 30 + (k // 3) * 36)}" width="40" height="30" rx="6" fill="{C["sun"]}" stroke="{C["navy"]}" stroke-width="3"/>' for k in range(6))
        if lb >= 0:
            a, b = xs[-1], xs[lb]
            y0 = cy + size / 2 + 20
            body += f'<path d="M{_f(a)} {_f(y0)} C{_f(a - 60)} {_f(h - 20)} {_f(b + 60)} {_f(h - 20)} {_f(b)} {_f(y0 + 6)}" fill="none" stroke="{C["coral"]}" stroke-width="10" stroke-dasharray="20 14" stroke-linecap="round"/>'
            body += f'<path d="M{_f(b - 22)} {_f(y0 + 30)} L{_f(b)} {_f(y0 + 4)} L{_f(b + 24)} {_f(y0 + 30)}" fill="none" stroke="{C["coral"]}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>'
    elif kind == "checklist":
        n = _int(spec.get("items"), 3, 12, 6)
        miss = _int(spec.get("highlight"), -1, n - 1, -1)
        top = 450 - (n * 58) / 2
        body = f'<rect width="{w}" height="{h}" fill="{C["sky"]}"/><rect y="{FLOOR + 60}" width="{w}" height="{H - FLOOR - 60}" fill="{C["floor"]}"/>'
        body += f'<g transform="rotate(-3 620 450)"><rect x="380" y="{_f(top - 70)}" width="480" height="{_f(n * 58 + 120)}" rx="18" fill="{C["wood"]}"/><rect x="400" y="{_f(top - 50)}" width="440" height="{_f(n * 58 + 80)}" rx="8" fill="{C["paper"]}"/><rect x="540" y="{_f(top - 90)}" width="160" height="44" rx="12" fill="{C["slate"]}"/>'
        for i in range(n):
            y = top + i * 58
            m = i == miss
            body += f'<rect x="430" y="{_f(y)}" width="36" height="36" rx="7" fill="{C["sun"] if m else C["paper"]}" stroke="{C["coral"] if m else C["blue"]}" stroke-width="{7 if m else 5}"/>'
            if not m:
                body += f'<path d="M437 {_f(y + 18)} L447 {_f(y + 28)} L461 {_f(y + 9)}" fill="none" stroke="{C["mint"]}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
            body += f'<rect x="488" y="{_f(y + 11)}" width="{[300, 240, 280, 220, 290, 250, 270, 230, 260, 300, 210, 240][i]}" height="14" rx="7" fill="{C["navy"]}" opacity="{.85 if m else .5}"/>'
        body += "</g>"
        if miss >= 0:
            body += badge("cross", 940, top + miss * 58 + 10, C, 46)
        people = _people(spec.get("people"), C)[:1]
        for p in people:
            body += person(1220, FLOOR + 90, 1.75, C, p["skin"], p["hair"], p["shirt"], "point" if p["pose"] == "stand" else p["pose"], p["outfit"], flip=True)
        body += badge(_enum(spec.get("badge"), BADGES, "none"), W - 120, 120, C) if miss < 0 else ""
    elif kind == "chart":
        trend = _enum(spec.get("trend"), TRENDS, "up")
        vals = {"up": [90, 140, 120, 200, 260], "down": [260, 210, 220, 140, 90], "flat": [150, 170, 140, 160, 150]}[trend]
        body = f'<rect width="{w}" height="{h}" fill="{C["sky"]}"/><rect y="{FLOOR}" width="{w}" height="{H - FLOOR}" fill="{C["floor"]}"/>'
        body += f'<rect x="300" y="120" width="760" height="520" rx="18" fill="{C["paper"]}" stroke="{C["slate"]}" stroke-width="10"/><path d="M380 560 H990 M380 560 V180" stroke="{C["navy"]}" stroke-width="8" stroke-linecap="round"/>'
        pts = []
        for i, v in enumerate(vals):
            x = 430 + i * 112
            body += f'<rect x="{x}" y="{560 - v}" width="64" height="{v}" rx="6" fill="{C["blue"]}"/>'
            pts.append((x + 32, 560 - v - 40))
        col = C["mint"] if trend == "up" else C["coral"] if trend == "down" else C["slate"]
        body += f'<path d="M{" L".join(f"{x} {y}" for x, y in pts)}" fill="none" stroke="{col}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/><circle cx="{pts[-1][0]}" cy="{pts[-1][1]}" r="14" fill="{col}"/>'
        body += f'<rect x="520" y="640" width="14" height="{FLOOR + 70 - 640}" fill="{C["slate"]}"/><rect x="826" y="640" width="14" height="{FLOOR + 70 - 640}" fill="{C["slate"]}"/>'
        for p in _people(spec.get("people"), C)[:1]:
            body += person(1230, FLOOR + 90, 1.75, C, p["skin"], p["hair"], p["shirt"], "point" if p["pose"] == "stand" else p["pose"], p["outfit"], flip=True)
        body += badge(_enum(spec.get("badge"), BADGES, "none"), W - 120, 120, C)
    elif kind == "compare":
        body = f'<rect width="{w}" height="{h}" fill="{C["sky"]}"/>'
        for i, side in enumerate(("left", "right")):
            sub = spec.get(side) if isinstance(spec.get(side), dict) else {}
            sub = {**sub, "setting": _enum(sub.get("setting") or spec.get("setting"), SETTINGS, "plain"), "badge": "none", "focus": ""}
            ox, k = 50 + i * 770, 780 / 900
            body += f'<clipPath id="cp{i}"><rect x="{ox}" y="60" width="730" height="780" rx="28"/></clipPath>'
            body += (f'<g clip-path="url(#cp{i})"><rect x="{ox}" y="60" width="730" height="780" fill="{C["sky"]}"/>'
                     f'<g transform="translate({_f(ox + 365 - 800 * k)},{_f(840 - 900 * k)}) scale({_f(k)})">{_scene_body(sub, C)}</g></g>')
            body += f'<rect x="{ox}" y="60" width="730" height="780" rx="28" fill="none" stroke="{C["paper"]}" stroke-width="12"/>'
            b = _enum((spec.get(side) or {}).get("badge") if isinstance(spec.get(side), dict) else "", BADGES, "check" if i == 0 else "cross")
            body += badge(b, ox + 365, 80, C, 50)
    else:
        body = _scene_body(spec, C)
    alt = html.escape(str(spec.get("alt") or "")[:500], quote=True)
    role = f'role="img" aria-label="{alt}"' if alt else 'aria-hidden="true"'
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {role}>{body}</svg>'
    return svg, w, h


# ---------------------------------------------------------------- free guess from a description (no AI)

def _has(text: str, *words: str) -> bool:
    return any(re.search(r"\b" + w, text) for w in words)


MIRROR = {"far_left": "far_right", "left": "right", "center_left": "center_right", "center": "center",
          "center_right": "center_left", "right": "left", "far_right": "far_left"}


def guess(description: str, alt: str = "") -> dict:
    """A reasonable scene from keywords, varied a little per picture so a course isn't all the same."""
    spec = _guess(description, alt)
    if not spec.get("alt"):
        spec["alt"] = describe(spec)
    n = zlib.crc32(f"{description}|{alt}".encode("utf-8"))
    shirts = ("blue", "coral", "mint", "navy", "slate")
    flip = n % 2 == 1

    def vary(people, props):
        for i, p in enumerate(people):
            p.setdefault("skin", (n >> (3 * i)) % 5 + 1)
            p.setdefault("shirt", shirts[(n >> (2 * i + 7)) % len(shirts)])
            if flip and spec["kind"] in ("scene", "compare"):
                p["position"] = MIRROR.get(p.get("position"), p.get("position"))
        if flip and spec["kind"] in ("scene", "compare"):
            for pr in props:
                pr["position"] = MIRROR.get(pr.get("position"), pr.get("position"))
                pr.setdefault("state", "normal")
    vary(spec.get("people") or [], spec.get("props") or [])
    if flip and spec.get("focus"):
        spec["focus"] = MIRROR.get(spec["focus"], spec["focus"])
    for side in ("left", "right"):
        if isinstance(spec.get(side), dict):
            vary(spec[side].get("people") or [], spec[side].get("props") or [])
    return spec


WHO = {"hi_vis": "a worker in a hi-vis vest", "hi_vis_hard_hat": "a worker in a hard hat", "visitor_vest": "a visitor in an orange vest",
       "office": "a colleague", "casual": "a person"}
DOING = {"clipboard": " holding a clipboard", "phone": " on the phone", "point": " pointing", "wave": " waving", "walk": " walking", "sit": " sitting", "stand": ""}
WHERE = {"office": "in an office", "warehouse": "in a warehouse aisle", "loading_dock": "at the loading dock", "home_night": "at home at night",
         "outdoors": "outdoors", "plain": ""}
THING = {"forklift": "a forklift", "desk": "a desk with a computer", "laptop_desk": "a laptop", "whiteboard": "a whiteboard", "pallet": "a pallet of boxes",
         "cone": "safety cones", "warning_sign": "a warning sign", "tag": "an out-of-service tag", "fire_extinguisher": "a fire extinguisher",
         "first_aid_kit": "a first aid kit", "meeting_table": "a meeting table", "plant": ""}


def describe(spec: dict) -> str:
    """Plain alt text for a drawing made without Vera, saying only what the drawing shows."""
    kind = spec.get("kind")
    if kind == "checklist":
        return "A checklist on a clipboard" + (" with one item not ticked" if spec.get("highlight", -1) >= 0 else "") + "."
    if kind == "chart":
        return {"up": "A bar chart with a rising trend.", "down": "A bar chart with a falling trend."}.get(spec.get("trend"), "A bar chart.")
    if kind == "flow":
        return f"A process of {len(spec.get('glyphs') or [])} steps joined by arrows."
    if kind == "icon":
        return f"An icon of a {(spec.get('glyphs') or ['star'])[0].replace('_', ' ')}."
    if kind == "compare":
        if (spec.get("left") or {}).get("badge") == "check":
            return "Two side-by-side pictures: the right way, marked with a tick, and the wrong way, marked with a cross."
        return "Two side-by-side pictures of " + ("workers in a warehouse." if (spec.get("left") or {}).get("setting") == "warehouse" else "colleagues at work.")
    parts = [WHO.get(p.get("outfit"), "a person") + DOING.get(p.get("pose"), "") for p in spec.get("people") or []]
    if parts and any(p.get("pose") == "sit" for p in spec.get("people") or []) and any(pr.get("name") == "forklift" for pr in spec.get("props") or []):
        parts = [x.replace(" sitting", " driving the forklift") for x in parts]
    things = [THING.get(pr.get("name"), "") for pr in spec.get("props") or []]
    things = [x for x in things if x and not (x == "a forklift" and any("driving" in p for p in parts))]
    items = parts + things
    text = ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + (items[-1] if items else "an empty room")
    where = WHERE.get(spec.get("setting"), "")
    out = f"Illustration of {text}" + (f" {where}" if where else "") + "."
    if spec.get("badge") == "warning":
        out += " A warning sign marks the hazard."
    return out


def _guess(description: str, alt: str = "") -> dict:
    t = f"{description} {alt}".lower()
    spec: dict = {"kind": "scene", "setting": "office", "people": [], "props": [], "focus": "", "badge": "none",
                  "glyphs": [], "highlight": -1, "loop_back_to": -1, "items": 6, "trend": "up", "alt": alt}
    hazard = _has(t, "hazard", "danger", "unsafe", "defect", "crack", "damag", "tip-over", "tip over", "near miss", "wrong", "incorrect", "warning")
    vest = "hi_vis" if _has(t, "warehouse", "forklift", "truck", "dock", "aisle", "operator", "site", "hi-vis", "vest", "pallet") else "office"
    trucks = _has(t, "forklift", "fork\\b", "forks\\b", "truck", "counterbalance", "pallet", "aisle", "racking", "warehouse", "dc floor", "dock")
    if _has(t, "checklist", "check list", "inspection form", "tick") and not trucks:
        return {**spec, "kind": "checklist", "items": 8, "highlight": 4 if hazard or _has(t, "miss", "skip") else -1,
                "people": [{"position": "right", "pose": "point", "outfit": vest}]}
    if _has(t, "chart\\b", "charts\\b", "graph\\b", "graphs\\b", "trend", "metric", "statistic") and not trucks:
        return {**spec, "kind": "chart", "trend": "down" if _has(t, "drop", "fall", "declin", "down") else "up",
                "people": [{"position": "right", "pose": "point", "outfit": vest}]}
    if _has(t, "flow\\b", "process\\b", "steps\\b", "timeline", "stages\\b") and not trucks:
        return {**spec, "kind": "flow", "glyphs": ["clipboard", "magnifier", "gear", "check"]}
    if _has(t, "split", "side by side", "two panel", "two-panel", "do and don", "versus", "vs\\.?", "left:", "correct and incorrect"):
        w = "warehouse" if vest == "hi_vis" else "office"
        side = {"setting": w, "people": [{"position": "center", "pose": "stand", "outfit": vest}], "props": []}
        judged = _has(t, "do and don", "correct and incorrect", "right and wrong", "right way", "wrong way", "safe and unsafe")
        return {**spec, "kind": "compare", "setting": w, "left": {**side, "badge": "check" if judged else "none"},
                "right": {**side, "people": [{"position": "center", "pose": "point", "outfit": vest, "shirt": "coral"}], "badge": "cross" if judged else "none"}}
    if _has(t, "icon", "symbol", "badge", "logo") and not trucks:
        g = next((k for k, ws in (("check", ("check mark", "tick")), ("warning", ("warning", "hazard")), ("clipboard", ("clipboard",)),
                                  ("tag", ("tag",)), ("lock", ("padlock", "lock")), ("clock", ("clock", "time"))) if any(w in t for w in ws)), "star")
        return {**spec, "kind": "icon", "glyphs": [g]}
    if trucks or _has(t, "load\\b"):
        dock = _has(t, "dock", "trailer")
        state = "raised_load" if _has(t, "raised", "height", "high", "beam level", "lift") else "lowered_load"
        spec.update(setting="loading_dock" if dock else "warehouse",
                    people=[{"position": "left", "pose": "clipboard" if _has(t, "checklist", "clipboard", "inspect", "walk") else "stand", "outfit": "hi_vis"}],
                    props=[{"name": "forklift", "position": "right", "state": "alert" if hazard else state}])
        if _has(t, "operator", "driving", "travel", "seat", "turn", "carrying"):
            spec["people"].append({"position": "right", "pose": "sit", "outfit": "hi_vis_hard_hat"})
        if _has(t, "pedestrian", "walk", "scanner", "visitor", "driver"):
            spec["people"].append({"position": "far_left", "pose": "walk" if "walk" in t else "phone", "outfit": "visitor_vest" if "visitor" in t else "hi_vis"})
        if hazard:
            spec.update(badge="warning", focus="right")
        return spec
    if _has(t, "night", "after-hours", "after hours", "at home", "evening"):
        return {**spec, "setting": "home_night", "people": [{"position": "center_left", "pose": "sit", "outfit": "casual"}],
                "props": [{"name": "laptop_desk", "position": "center_left", "state": "alert" if hazard or "alert" in t else "normal"}]}
    if _has(t, "outdoor", "yard", "car park", "parking", "street", "road"):
        return {**spec, "setting": "outdoors", "people": [{"position": "left", "pose": "stand", "outfit": vest}, {"position": "right", "pose": "wave", "outfit": vest}],
                "props": [{"name": "cone", "position": "center", "state": "normal"}]}
    people = [{"position": "left", "pose": "point", "outfit": "office"}, {"position": "right", "pose": "stand", "outfit": "office", "shirt": "coral"}]
    if _has(t, "team", "meeting", "colleagues", "group", "discuss"):
        people.append({"position": "center", "pose": "stand", "outfit": "office", "shirt": "mint"})
        spec.update(props=[{"name": "whiteboard", "position": "center", "state": "chart" if "result" in t else "normal"}],
                    people=[people[0], people[1]] + ([] if "whiteboard" in t else []))
        return spec
    if _has(t, "computer", "screen", "desk", "laptop", "email", "system", "terminal"):
        return {**spec, "people": [{"position": "center_left", "pose": "sit", "outfit": "office"}],
                "props": [{"name": "desk", "position": "center_left", "state": "alert" if hazard else "normal"}, {"name": "plant", "position": "far_right"}],
                "badge": "warning" if hazard else "none"}
    if _has(t, "phone", "call"):
        people[0]["pose"] = "phone"
    spec.update(people=people[:2], props=[{"name": "plant", "position": "far_right"}], badge="warning" if hazard else "none")
    return spec
