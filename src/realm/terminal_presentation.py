"""Optional ANSI/Mudlet presentation for the generic REALM Telnet adapter.

Color ideas and the named palette adapt the public-safe terminal pilot at
thebrazenbeard/driftspace PR #4 head ffd1deab4d27. No setting, actor,
source-text, private crew receipt or gameplay state is imported.
Only a user-opted-in terminal session renders escape sequences.
"""
from __future__ import annotations

import re

PROSE_COLORS = {
    # Classic ANSI family, tuned for readability on a dark MUD background.
    "black": "30;47",                 # black on pale background
    "red": "38;2;255;105;97",
    "green": "38;2;90;200;130",
    "yellow": "38;2;255;225;80",
    "blue": "38;2;100;160;255",
    "magenta": "38;2;255;0;255",
    "cyan": "38;2;0;255;255",
    "white": "38;2;245;245;245",
    "bright black": "38;2;144;144;144",
    "bright red": "38;2;255;85;85",
    "bright green": "38;2;120;255;140",
    "bright yellow": "38;2;255;240;120",
    "bright blue": "38;2;98;187;255",
    "bright magenta": "38;2;255;125;255",
    "bright cyan": "38;2;120;255;255",
    "bright white": "38;2;255;255;255",
    # Extended color words and UK/US spellings used in descriptive prose.
    "copper": "38;2;184;115;51",
    "amber": "38;2;255;191;0",
    "silver": "38;2;192;192;192",
    "gold": "38;2;255;215;0",
    "orange": "38;2;255;160;65",
    "purple": "38;2;187;134;252",
    "violet": "38;2;186;145;255",
    "pink": "38;2;255;105;180",
    "light pink": "38;2;255;182;193",
    "dark pink": "38;2;225;100;150",
    "teal": "38;2;0;180;170",
    "turquoise": "38;2;64;224;208",
    "aqua": "38;2;0;255;255",
    "navy": "38;2;106;148;220",
    "indigo": "38;2;140;118;238",
    "lime": "38;2;170;255;90",
    "olive": "38;2;175;177;90",
    "brown": "38;2;173;112;70",
    "crimson": "38;2;230;50;80",
    "scarlet": "38;2;255;64;64",
    "maroon": "38;2;185;95;116",
    "cream": "38;2;245;232;190",
    "beige": "38;2;226;208;175",
    "slate": "38;2;150;165;180",
    "grey": "38;2;170;170;170",
    "gray": "38;2;170;170;170",
    "light grey": "38;2;211;211;211",
    "light gray": "38;2;211;211;211",
    "light-grey": "38;2;211;211;211",
    "light-gray": "38;2;211;211;211",
    "dark grey": "38;2;144;144;144",
    "dark gray": "38;2;144;144;144",
    "dark-grey": "38;2;144;144;144",
    "dark-gray": "38;2;144;144;144",
    "light blue": "38;2;150;205;255",
    "light green": "38;2;144;238;144",
    "light cyan": "38;2;165;243;252",
}

# Longest-first phrases and word boundaries protect multiword aliases.
_PROSE_WORD = re.compile(
    r"\b(?:" + "|".join(
        re.escape(name) for name in sorted(PROSE_COLORS, key=len, reverse=True)
    ) + r")\b", re.IGNORECASE,
)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_LABELS = (
    ("Exits:", "1;33"),
    ("Items:", "1;32"),
    ("Also here:", "1;36"),
    ("You carry:", "1;32"),
    ("You take ", "1;32"),
    ("You drop ", "1;32"),
    ("You say:", "1;35"),
    ("You yell:", "1;35"),
    ("[OOC]", "1;35"),
)


def _safe_text(text: str) -> str:
    """Strip source-supplied terminal controls before adding trusted SGR."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    return _CONTROL.sub("", text)


def _paint(value: str, sgr: str) -> str:
    return "\x1b[" + sgr + "m" + value + "\x1b[0m"


def _highlight_room_description(line: str) -> str:
    return _PROSE_WORD.sub(
        lambda hit: _paint(hit.group(0), PROSE_COLORS[hit.group(0).casefold()]),
        line,
    )


def _color_label(line: str) -> str:
    for prefix, sgr in _LABELS:
        if line.startswith(prefix):
            label = prefix.rstrip()
            value = line[len(prefix):].strip()
            return _paint(label, "37") + (" " + _paint(value, sgr) if value else "")
    return line


def render_terminal(text: str, *, ansi: bool = False, room: bool = False) -> str:
    """Render text only; never change a world command, actor or receipt.

    A room view must be explicitly indicated by its host caller AND contain
    its Engine-generated Exits label. Never infer room prose from chat input.
    """
    if not ansi:
        return text
    lines = _safe_text(text).split("\n")
    room_view = room and len(lines) >= 3 and any(
        line.startswith("Exits:") for line in lines[2:]
    )
    in_description = room_view
    output = []
    for index, line in enumerate(lines):
        if room_view and index == 0:
            output.append(_paint(line, "1;36"))
            continue
        if in_description and line.startswith("Exits:"):
            in_description = False
        if in_description:
            output.append(_highlight_room_description(line))
        else:
            output.append(_color_label(line))
    return "\n".join(output)


def palette_preview(*, ansi: bool = False) -> str:
    """Named swatches; plain text until a player enables terminal ANSI."""
    groups = (
        ("Classic", ("black", "red", "green", "yellow", "blue",
                     "magenta", "cyan", "white")),
        ("Bright", ("bright black", "bright red", "bright green",
                    "bright yellow", "bright blue", "bright magenta",
                    "bright cyan", "bright white")),
        ("Extended", ("pink", "light grey", "dark grey", "copper",
                      "amber", "silver", "gold", "teal", "turquoise",
                      "purple", "orange")),
    )
    lines = ["REALM terminal color palette"]
    for title, names in groups:
        words = [_paint(name, PROSE_COLORS[name]) if ansi else name for name in names]
        lines.append(title + ": " + "   ".join(words))
    lines.append("Named colors are highlighted only in room descriptions.")
    return "\n".join(lines)
