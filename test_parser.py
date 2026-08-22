from parser import base_symbol, parse_alert

SAMPLE = """🔔 #OPUSDT OPUSDT, Пересечение 0.10282
-  exchange:  #BybitFutures
-  trend: 📈
-  price: 0.10277"""

FLAT = "🔔 #OPUSDT OPUSDT, Пересечение 0.10282 -  exchange:  #BybitFutures -  trend: 📈 -  price: 0.10277"

DOWN = """🔔 #BOMEUSDT BOMEUSDT, Пересечение 0.00412
-  exchange:  #BinanceFutures
-  trend: 📉
-  price: 0.00409"""


def test_multiline():
    assert parse_alert(SAMPLE) == "OP 📈 0.10277"


def test_flat_one_line():
    assert parse_alert(FLAT) == "OP 📈 0.10277"


def test_down_trend():
    assert parse_alert(DOWN) == "BOME 📉 0.00409"


def test_non_alert_is_skipped():
    assert parse_alert("Бот запущен") is None
    assert parse_alert("") is None


def test_base_symbol_strips_quote():
    assert base_symbol("OPUSDT") == "OP"
    assert base_symbol("ETHBTC") == "ETH"
    assert base_symbol("USDT") == "USDT"
