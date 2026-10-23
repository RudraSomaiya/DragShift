"""What the Add BG dialog and its worker agree on (PRD §8.6): the swatches, the ratios, the
defaults and the geometry of the finished picture.

The dialog paints a preview with QPainter and the worker renders the real thing with Pillow, from
the same params and the same numbers from here. Sizes are in pixels of the *source* picture, so
the preview scales them by its own zoom. Pure Python: both processes import it.

Params (``Op`` ``tools.image.add_bg``)::

    {"bg": {"kind": "gradient", "colors": ["#A1C4FD", "#C2E9FB"]}   # top left -> bottom right
          | {"kind": "solid", "color": "#FFFFFF"}
          | {"kind": "image", "path": "C:/pics/wall.jpg"},          # covers the canvas
     "padding": 96, "corners": 24, "shadow": 24, "ratio": "Auto"}
"""

GRADIENTS = {
    "Glacier": ("#A1C4FD", "#C2E9FB"),
    "Aurora": ("#7F7FD5", "#91EAE4"),
    "Mint": ("#D4FC79", "#96E6A1"),
    "Dusk": ("#7F53AC", "#647DEE"),
    "Mono": ("#F5F5F5", "#BDBDBD"),
    "Peach": ("#FFECD2", "#FCB69F"),
    "Ocean": ("#2193B0", "#6DD5ED"),
    "Candy": ("#FBC2EB", "#A6C1EE"),
}
SOLIDS = {
    "White": "#FFFFFF",
    "Light grey": "#E5E7EB",
    "Charcoal": "#1F2937",
    "Black": "#000000",
    "Blue": "#3B82F6",
    "Green": "#22C55E",
    "Orange": "#F59E0B",
    "Pink": "#EC4899",
}
RATIOS = {"Auto": None, "1:1": 1.0, "16:9": 16 / 9, "4:5": 4 / 5}

MAX_PADDING, MAX_CORNERS, MAX_SHADOW = 400, 200, 100
MAX_PIXELS = 100_000_000  # the canvas of a result (an RGB copy is 3 bytes each); more is refused

DEFAULTS = {
    "bg": {"kind": "gradient", "colors": list(GRADIENTS["Glacier"])},
    "padding": 96,
    "corners": 24,
    "shadow": 24,
    "ratio": "Auto",
}


def layout(
    size: tuple[int, int], padding: int, ratio: str
) -> tuple[tuple[int, int], tuple[int, int]]:
    """The canvas ``(width, height)`` and where the picture's top left sits on it. The padding
    goes round the picture; a ratio then grows the canvas on one axis (never crops)."""
    w, h = size
    cw, ch = w + 2 * padding, h + 2 * padding
    wanted = RATIOS[ratio]
    if wanted:
        if cw / ch < wanted:
            cw = round(ch * wanted)
        else:
            ch = round(cw / wanted)
    return (cw, ch), ((cw - w) // 2, (ch - h) // 2)


def corner_radius(size: tuple[int, int], corners: int) -> int:
    """The corner radius, no more than half the shorter side."""
    return max(0, min(corners, min(size) // 2))


def shadow_look(shadow: int) -> tuple[float, float, float]:
    """``(opacity 0..1, blur sigma, drop)`` in source pixels for the Shadow slider."""
    return shadow / 100 * 0.6, shadow / 2, shadow / 3
