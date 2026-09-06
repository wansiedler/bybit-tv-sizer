"""Render a trade-entry chart: candles, entry/TP/SL lines, risk zones.

A real TradingView screenshot needs a logged-in browser; a relay in a slim
container does not have one. Drawing the same picture ourselves from Bybit's
public kline data needs only Pillow, and the result carries everything the
screenshot would: where the entry is, where the exits are, which way the
trade points, and how the risk compares to the reward.
"""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1000, 640
MARGIN = 16
PRICE_GUTTER = 130  # right-hand strip where the level labels live

BACKGROUND = (19, 23, 34)
UP = (38, 166, 154)
DOWN = (239, 83, 80)
ENTRY = (208, 214, 222)
GRID = (34, 40, 54)
TEXT = (208, 214, 222)
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
    take_profit: float | None,
    stop_loss: float | None,
) -> bytes:
    """The chart as PNG bytes. Raises on empty candles: nothing to draw."""
    if not candles:
        raise ValueError("no candles")

    levels = [entry, *([take_profit] if take_profit else []), *([stop_loss] if stop_loss else [])]
    lowest = min(min(c.low for c in candles), *levels)
    highest = max(max(c.high for c in candles), *levels)
    to_y = _scale(lowest, highest)

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image, "RGBA")

    chart_right = WIDTH - PRICE_GUTTER
    # The zones sit under the candles, TradingView-style: green between entry
    # and target, red between entry and stop, drawn across the whole chart.
    if take_profit:
        draw.rectangle(
            (MARGIN, to_y(max(entry, take_profit)), chart_right, to_y(min(entry, take_profit))),
            fill=PROFIT_FILL,
        )
    if stop_loss:
        draw.rectangle(
            (MARGIN, to_y(max(entry, stop_loss)), chart_right, to_y(min(entry, stop_loss))),
            fill=RISK_FILL,
        )

    step = (chart_right - MARGIN) / len(candles)
    body = max(2.0, step * 0.6)
    for i, candle in enumerate(candles):
        x = MARGIN + step * i + step / 2
        color = UP if candle.close >= candle.open else DOWN
        draw.line((x, to_y(candle.high), x, to_y(candle.low)), fill=color, width=1)
        top, bottom = sorted((to_y(candle.open), to_y(candle.close)))
        draw.rectangle((x - body / 2, top, x + body / 2, max(bottom, top + 1)), fill=color)

    def level(price: float, color: tuple[int, int, int], tag: str) -> None:
        y = to_y(price)
        for x in range(MARGIN, chart_right, 12):  # dashed, so candles stay readable
            draw.line((x, y, x + 6, y), fill=color, width=2)
        draw.text((chart_right + 8, y - 7), f"{tag} {price:g}", fill=color)

    level(entry, ENTRY, "in")
    if take_profit:
        level(take_profit, UP, "tp")
    if stop_loss:
        level(stop_loss, DOWN, "sl")

    arrow = "▲" if side == "long" else "▼"
    draw.text((MARGIN + 6, MARGIN + 4), f"{symbol} · {side} {arrow}", fill=TEXT)

    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
