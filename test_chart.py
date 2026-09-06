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
