"""Tests for the entry-chart renderer. Pixels are sampled, not eyeballed."""

import pytest
from PIL import Image

import chart
from chart import Candle

CANDLES = [
    Candle(0.160, 0.165, 0.158, 0.163),
    Candle(0.163, 0.168, 0.162, 0.162),  # a red one
    Candle(0.162, 0.170, 0.161, 0.169),
]


def load(png: bytes) -> Image.Image:
    from io import BytesIO

    return Image.open(BytesIO(png))


def test_render_returns_a_png_of_the_advertised_size():
    png = chart.render("FARTCOIN", "long", CANDLES, 0.162, 0.170, 0.158)

    assert png.startswith(b"\x89PNG")
    assert load(png).size == (chart.WIDTH, chart.HEIGHT)


def test_render_paints_both_zones():
    png = chart.render("FARTCOIN", "long", CANDLES, 0.162, 0.170, 0.158)

    colors = {rgb for _, rgb in load(png).getcolors(maxcolors=100000)}
    # The translucent fills blend with the background into distinct shades.
    assert len(colors) > 4
    assert chart.BACKGROUND in colors


def test_render_survives_missing_tp_and_sl():
    png = chart.render("FARTCOIN", "short", CANDLES, 0.162, None, None)

    assert png.startswith(b"\x89PNG")


def test_render_survives_flat_prices():
    flat = [Candle(1.0, 1.0, 1.0, 1.0)] * 3

    assert chart.render("X", "long", flat, 1.0, None, None).startswith(b"\x89PNG")


def test_render_survives_zero_prices():
    zero = [Candle(0.0, 0.0, 0.0, 0.0)] * 3

    assert chart.render("X", "long", zero, 0.0, None, None).startswith(b"\x89PNG")


def test_render_refuses_an_empty_chart():
    with pytest.raises(ValueError, match="no candles"):
        chart.render("X", "long", [], 1.0, None, None)


@pytest.mark.parametrize(
    ("side", "exit_price"),
    [
        ("long", 0.170),  # long, exited higher: profit colors
        ("long", 0.150),  # long, exited lower: loss colors
        ("short", 0.150),  # short, exited lower: profit colors
    ],
)
def test_render_draws_a_finished_trade(side, exit_price):
    png = chart.render(
        "FARTCOIN",
        side,
        CANDLES,
        0.162,
        entry_index=0,
        exit_index=2,
        exit_price=exit_price,
        pad_right=2,
    )

    assert png.startswith(b"\x89PNG")


def test_render_zones_start_at_the_entry_bar():
    """Left of the entry bar the zone fill must not appear."""
    entry, tp = 0.162, 0.170
    png = chart.render("FARTCOIN", "long", CANDLES, entry, tp, None, entry_index=2, pad_right=10)

    image = load(png).convert("RGB")
    # Sample inside the profit zone's price band: right of the entry bar the
    # fill tints the background; left of it the background stays clean.
    slots = len(CANDLES) + 10
    step = (chart.WIDTH - chart.PRICE_GUTTER - chart.MARGIN) / slots
    y = chart.HEIGHT // 3  # inside the entry..tp band for these numbers
    left = image.getpixel((int(chart.MARGIN + step * 0.5), y))
    right = image.getpixel((int(chart.MARGIN + step * (slots - 1)), y))

    assert left == chart.BACKGROUND
    assert right != chart.BACKGROUND
