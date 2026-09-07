"""Render trade charts: candles, entry/TP/SL lines, position zones.

A real TradingView screenshot needs a logged-in browser; a relay in a slim
container does not have one. Drawing the same picture ourselves from Bybit's
public kline data needs only Pillow, and the result carries everything the
screenshot would: where the trade started, where it points, where it ended.

The zones behave like TradingView's position tool: they begin at the entry
bar and run to the exit bar — or to the right edge while the trade is still
open (`pad_right` leaves empty future for them to stretch into).
"""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# The bundled scalable font: no system fonts to rely on in a slim image, and
# no glyphs outside basic latin — so labels stick to plain words, no arrows.
TITLE_FONT = ImageFont.load_default(30)
LABEL_FONT = ImageFont.load_default(20)

# TradingView-like proportions: tall enough that candles keep their shape
# even when a distant TP or SL stretches the price scale.
WIDTH, HEIGHT = 1920, 1280
MARGIN = 16
PRICE_GUTTER = 160  # right-hand strip where the level labels live

BACKGROUND = (19, 23, 34)
UP = (38, 166, 154)
DOWN = (239, 83, 80)
ENTRY = (208, 214, 222)
TEXT = (208, 214, 222)
GRID = (34, 40, 54)
GRID_TEXT = (120, 128, 144)
# Zone fills stay translucent so the candles read through them.
PROFIT_FILL = (38, 166, 154, 46)
RISK_FILL = (239, 83, 80, 46)


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float


def _scale(low: float, high: float):
    """Map price -> y pixel, with a little headroom above and below."""
    pad = (high - low) * 0.06 or high * 0.001 or 1.0
    top, bottom = high + pad, low - pad

    def to_y(price: float) -> float:
        return MARGIN + (top - price) * (HEIGHT - 2 * MARGIN) / (top - bottom)

    return to_y


def render(
    symbol: str,
    side: str,
    candles: list[Candle],
    entry: float,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    *,
    entry_index: int = 0,
    exit_index: int | None = None,
    exit_price: float | None = None,
    pad_right: int = 0,
    timeframe: str = "",
    entry_note: str = "",
    tp_note: str = "",
    sl_note: str = "",
    exit_note: str = "",
    plain: bool = False,
) -> bytes:
    """The chart as PNG bytes. Raises on empty candles: nothing to draw.

    `entry_index`/`exit_index` are bar positions; zones and the entry line
    span exactly that range. No `exit_index` means the trade is still open
    and everything runs to the right edge.
    """
    if not candles:
        raise ValueError("no candles")

    levels = [entry, take_profit, stop_loss, exit_price]
    prices = [price for price in levels if price is not None]
    lowest = min(min(c.low for c in candles), *prices)
    highest = max(max(c.high for c in candles), *prices)
    to_y = _scale(lowest, highest)

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image, "RGBA")

    chart_right = WIDTH - PRICE_GUTTER
    slots = len(candles) + pad_right
    step = (chart_right - MARGIN) / slots

    # A faint price grid, TradingView-style, under everything else.
    span = (highest - lowest) or 1.0
    for i in range(1, 8):
        price = lowest + span * i / 8
        y = to_y(price)
        draw.line((MARGIN, y, chart_right, y), fill=GRID, width=1)
        draw.text((chart_right + 8, y - 8), f"{price:.4g}", fill=GRID_TEXT, font=LABEL_FONT)

    def x_of(index: int) -> float:
        return MARGIN + step * index + step / 2

    # Bar edges, not centers: a trade entered and exited within one bar still
    # gets a zone one full bar wide instead of a zero-width sliver.
    zone_left = x_of(entry_index) - step / 2
    zone_right = x_of(exit_index) + step / 2 if exit_index is not None else chart_right

    def zone(a: float, b: float, fill: tuple[int, int, int, int]) -> None:
        draw.rectangle((zone_left, to_y(max(a, b)), zone_right, to_y(min(a, b))), fill=fill)

    # TradingView-style: green between entry and target, red between entry
    # and stop. A finished trade shows one zone, colored by how it went; its
    # TP and SL stay as lines so the outcome shading remains readable.
    if exit_price is not None:
        won = (exit_price >= entry) == (side == "long")
        zone(entry, exit_price, PROFIT_FILL if won else RISK_FILL)
    else:
        if take_profit:
            zone(entry, take_profit, PROFIT_FILL)
        if stop_loss:
            zone(entry, stop_loss, RISK_FILL)

    # At TradingView-zoom densities a bar is a single pixel column.
    body = max(1.0, step * 0.7)
    for i, candle in enumerate(candles):
        x = x_of(i)
        color = UP if candle.close >= candle.open else DOWN
        draw.line((x, to_y(candle.high), x, to_y(candle.low)), fill=color, width=1)
        top, bottom = sorted((to_y(candle.open), to_y(candle.close)))
        draw.rectangle((x - body / 2, top, x + body / 2, max(bottom, top + 1)), fill=color)

    def level(price: float, color: tuple[int, int, int], tag: str, note: str = "") -> None:
        y = to_y(price)
        for x in range(int(zone_left), int(zone_right) + 6, 12):  # dashed
            draw.line((x, y, x + 6, y), fill=color, width=2)
        draw.text((chart_right + 8, y - 8), f"{tag} {price:g}", fill=color, font=LABEL_FONT)
        if note:
            # The figure sits right against its line, inside the zone. ASCII
            # only: the bundled font has no cyrillic or typographic minus.
            width = draw.textlength(note, font=LABEL_FONT)
            draw.text((chart_right - width - 12, y - 30), note, fill=color, font=LABEL_FONT)

    if not plain:
        level(entry, ENTRY, "in", entry_note)
    if take_profit:
        level(take_profit, UP, "tp", tp_note)
    if stop_loss:
        level(stop_loss, DOWN, "sl", sl_note)
    if exit_price is not None:
        won = (exit_price >= entry) == (side == "long")
        level(exit_price, UP if won else DOWN, "out", exit_note)

    title = f"{symbol} · {side}" if side else symbol
    if timeframe:
        title += f" · {timeframe}"
    draw.text((MARGIN + 6, MARGIN + 4), title, fill=TEXT, font=TITLE_FONT)

    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def side_by_side(pngs: list[bytes]) -> bytes:
    """Several rendered charts pasted into one wide picture."""
    images = [Image.open(BytesIO(png)) for png in pngs]
    canvas = Image.new("RGB", (sum(i.width for i in images), max(i.height for i in images)))
    x = 0
    for image in images:
        canvas.paste(image, (x, 0))
        x += image.width
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
