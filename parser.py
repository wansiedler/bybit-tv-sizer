"""Parse LEXX Draco alert messages into a compact form.

Input example:
    🔔 #OPUSDT OPUSDT, Пересечение 0.10282
    -  exchange:  #BybitFutures
    -  trend: 📈
    -  price: 0.10277

Output: "OP 📈 0.10277"
"""

import re

# Quote assets stripped from the pair to get the base symbol.
QUOTES = ("USDT", "BUSD", "USDC", "FDUSD", "TUSD", "BTC", "ETH")

_SYMBOL_RE = re.compile(r"#([A-Z0-9]{2,20})\b")
_TREND_RE = re.compile(r"trend\s*:\s*(\S+)", re.IGNORECASE)
_PRICE_RE = re.compile(r"price\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)


def base_symbol(pair: str) -> str:
    for quote in QUOTES:
        if pair.endswith(quote) and len(pair) > len(quote):
            return pair[: -len(quote)]
    return pair


def parse_alert(text: str) -> str | None:
    """Return the compact line, or None if the message is not a price alert."""
    if not text:
        return None

    symbol_match = _SYMBOL_RE.search(text)
    trend_match = _TREND_RE.search(text)
    price_match = _PRICE_RE.search(text)
    if not (symbol_match and trend_match and price_match):
        return None

    symbol = base_symbol(symbol_match.group(1))
    trend = trend_match.group(1)
    price = price_match.group(1).replace(",", ".")
    return f"{symbol} {trend} {price}"
