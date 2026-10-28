"""avalanche.run palette (apps/console/src/index.css) as Textual themes + Rich palettes."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.theme import Theme

DARK = dict(
    accent="#6E73FF",
    page="#0E0F16",
    card="#15161F",
    muted_bg="#1B1C27",
    ink="#E8E8F0",
    muted="#9A9AA8",
    hairline="#3A3B4A",
    tint="#1C1E3A",
    danger="#FF7A6E",
    warning="#E8B04A",
    success="#3FC168",
)
LIGHT = dict(
    accent="#363CFF",
    page="#FEFEFE",
    card="#FEFEFE",
    muted_bg="#F2F2F6",
    ink="#1F202D",
    muted="#61616C",
    hairline="#BEBEBE",
    tint="#F5F5FF",
    danger="#B42318",
    warning="#8A5A00",
    success="#00761E",
)


def textual_theme(name: str, p: dict, dark: bool) -> Theme:
    return Theme(
        name=name,
        primary=p["accent"],
        secondary=p["accent"],
        accent=p["accent"],
        warning=p["warning"],
        error=p["danger"],
        success=p["success"],
        foreground=p["ink"],
        background=p["page"],
        surface=p["card"],
        panel=p["muted_bg"],
        dark=dark,
        variables={
            "border": p["hairline"],
            "border-blurred": p["hairline"],
            "block-cursor-background": p["tint"],
            "block-cursor-foreground": p["accent"],
            "block-cursor-text-style": "none",
            "block-cursor-blurred-background": p["tint"],
            "block-cursor-blurred-foreground": p["ink"],
            "block-hover-background": p["muted_bg"],
            "footer-background": p["page"],
            "footer-key-foreground": p["accent"],
            "text-muted": p["muted"],
            "foreground-muted": p["muted"],
            "scrollbar": p["hairline"],
            "scrollbar-hover": p["muted"],
            "scrollbar-active": p["accent"],
            "scrollbar-background": p["page"],
            "scrollbar-corner-color": p["page"],
            "hairline": p["hairline"],
            "tint": p["tint"],
        },
    )


THEMES = {
    "dark": textual_theme("avalanche-run-dark", DARK, True),
    "light": textual_theme("avalanche-run-light", LIGHT, False),
}


def palette(mode: str) -> dict:
    p = DARK if mode == "dark" else LIGHT
    return {
        "danger": p["danger"],
        "active": p["accent"],
        "waiting": p["warning"],
        "success": p["ink"],
        "neutral": p["muted"],
        "ok": p["success"],
        "ink": p["ink"],
        "muted": p["muted"],
        "accent": p["accent"],
        "hairline": p["hairline"],
        "page": p["page"],
        "tint": p["tint"],
        "card": p["card"],
    }


# Avalanche's DAG renderer styles with its glacier palette; map each colour onto ours.
_ICE = {
    "#2f254c": "page",
    "#b898e0": "muted",
    "#90c8e0": "muted",
    "#60dce4": "ok",
    "#b8f4ff": "accent",
    "#f0faff": "ink",
    "#f8d078": "waiting",
    "#f87898": "danger",
}


def _map_color(color, pal):
    if color is None:
        return None
    name = color.name.lower() if color.name.startswith("#") else None
    if name is None and color.triplet is not None:
        name = color.triplet.hex.lower()
    return pal[_ICE[name]] if name in _ICE else None


def recolor(text: Text, pal: dict) -> Text:
    """Return a copy of Avalanche-rendered DAG text in the avalanche.run palette."""
    out = Text(text.plain, no_wrap=True, end="")
    for span in text.spans:
        style = span.style if isinstance(span.style, Style) else Style.parse(str(span.style))
        fg, bg = _map_color(style.color, pal), _map_color(style.bgcolor, pal)
        selected = style.bgcolor is not None
        new = Style(
            color=(pal["page"] if selected else fg) or style.color,
            bgcolor=pal["accent"] if selected else bg,
            bold=style.bold,
            dim=False if fg else style.dim,
        )
        out.stylize(new, span.start, span.end)
    return out
