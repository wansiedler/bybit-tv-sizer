"""Tests for the host-side TradingView capture service.

The browser is faked throughout: what is worth asserting is the decisions
this module makes around the browser — which pane, what size, when to refuse
— and those are the part a real Chrome would hide rather than prove.
"""

import sys
import types
import urllib.error
import urllib.request

import pytest

import tv_capture


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(tv_capture, "TV_SHOT_SECRET", "s3cret")
    monkeypatch.setattr(tv_capture, "TV_SHOT_LAYOUT", "https://tv.test/chart/ABC/")
    monkeypatch.setattr(tv_capture, "TV_SHOT_PORT", 0)  # ephemeral: no clash in CI
    monkeypatch.setattr(tv_capture, "TV_SHOT_BIND", "127.0.0.1")
    monkeypatch.setattr(tv_capture, "TV_SHOT_INTERVAL", "")
    monkeypatch.setattr(tv_capture, "TV_SHOT_EXCHANGE", "BYBIT")
    monkeypatch.setattr(tv_capture, "TV_SHOT_SETTLE", 0.0)
    monkeypatch.setattr(tv_capture, "TV_SHOT_WIDTH", 760)
    monkeypatch.setattr(tv_capture, "TV_SHOT_HEIGHT", 460)


# --- the fake browser ------------------------------------------------------


class FakePane:
    def __init__(self, head, box, shot=b"PNG"):
        self._head, self._box, self._shot = head, box, shot

    def inner_text(self):
        return self._head

    def bounding_box(self):
        return self._box

    def screenshot(self):
        return self._shot


class FakeLocator:
    def __init__(self, panes):
        self._panes = panes

    def count(self):
        return len(self._panes)

    def nth(self, index):
        return self._panes[index]


def row_of(heads, width=377.0, height=1119.0):
    """A 1xN grid of panes, the shape the real layout is in today."""
    return [
        FakePane(head, {"x": 56 + i * width, "y": 42, "width": width, "height": height})
        for i, head in enumerate(heads)
    ]


class FakePage:
    """Enough of a Playwright page to drive every branch of a shot."""

    def __init__(self, panes_for, closed=None):
        self._panes_for = panes_for  # url -> list[FakePane]
        self._url = None
        self.viewports = []
        self.closed = closed if closed is not None else []

    def set_viewport_size(self, size):
        self.viewports.append((size["width"], size["height"]))

    def goto(self, url, wait_until=None):
        self._url = url

    def wait_for_selector(self, selector, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def locator(self, selector):
        assert selector == tv_capture.PANE
        return FakeLocator(self._panes_for(self._url))

    def close(self):
        self.closed.append(True)


class FakeContext:
    def __init__(self, page, names=("sessionid", "sessionid_sign")):
        self._page, self._names = page, names

    def cookies(self, url):
        return [{"name": name} for name in self._names]

    def new_page(self):
        return self._page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]


def shooter_for(page, names=("sessionid", "sessionid_sign")):
    browser = FakeBrowser(FakeContext(page, names))
    return tv_capture.Shooter(
        connect=lambda url: browser, alive=lambda url: True, launch=lambda: None
    )


# --- the small decisions ---------------------------------------------------


def test_enabled_needs_both_a_secret_and_a_layout(monkeypatch):
    assert tv_capture.enabled() is True
    monkeypatch.setattr(tv_capture, "TV_SHOT_SECRET", "")
    assert tv_capture.enabled() is False
    monkeypatch.setattr(tv_capture, "TV_SHOT_SECRET", "s3cret")
    monkeypatch.setattr(tv_capture, "TV_SHOT_LAYOUT", "")
    assert tv_capture.enabled() is False


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("symbol=solusdt.p", "SOLUSDT.P"),
        ("symbol=BTCUSDT", "BTCUSDT"),
        ("symbol= SOLUSDT.P ", "SOLUSDT.P"),
        ("", ""),
        ("symbol=", ""),
        # Not tickers: this string is about to be spliced into a URL.
        ("symbol=../../etc/passwd", ""),
        ("symbol=SOL USDT", ""),
        ("symbol=A", ""),
        ("symbol=" + "X" * 20, ""),
    ],
)
def test_wanted_takes_tickers_and_nothing_else(query, expected):
    assert tv_capture.wanted(query) == expected


def test_grid_reads_the_layout_off_the_pane_corners():
    assert tv_capture.grid([box.bounding_box() for box in row_of(["A", "B", "C"])]) == (3, 1)
    four = [
        {"x": 0, "y": 0, "width": 1, "height": 1},
        {"x": 10, "y": 0, "width": 1, "height": 1},
        {"x": 0, "y": 10, "width": 1, "height": 1},
        {"x": 10, "y": 10, "width": 1, "height": 1},
    ]
    assert tv_capture.grid(four) == (2, 2)


def test_viewport_sizes_the_window_so_one_pane_lands_on_target():
    # 5 columns of 377 wide inside a 2000 probe: 115px of that window is
    # toolbar. Five 760-wide panes plus the same toolbar is 3915.
    assert tv_capture.viewport((2000, 1200), (377.0, 1119.0), (5, 1)) == (3915, 541)


def test_pick_matches_a_pane_exactly_never_by_prefix():
    heads = ["FARTCOINUSDT.P", "ARBUSDT.P", "NEARUSDT.P"]
    assert tv_capture.pick(heads, "ARBUSDT.P") == 1
    assert tv_capture.pick(heads, "ARB") is None
    assert tv_capture.pick(heads, "SOLUSDT.P") is None


def test_override_url_asks_for_one_symbol_and_optionally_an_interval(monkeypatch):
    assert tv_capture.override_url("https://tv.test/chart/ABC/", "SOLUSDT.P") == (
        "https://tv.test/chart/ABC/?symbol=BYBIT%3ASOLUSDT.P"
    )
    monkeypatch.setattr(tv_capture, "TV_SHOT_INTERVAL", "15")
    assert tv_capture.override_url("https://tv.test/chart/ABC/", "SOLUSDT.P") == (
        "https://tv.test/chart/ABC/?symbol=BYBIT%3ASOLUSDT.P&interval=15"
    )


# --- Chrome ownership ------------------------------------------------------


def test_cdp_alive_is_whether_devtools_answers():
    assert tv_capture.cdp_alive("http://x", opener=lambda url, timeout: _closing()) is True

    def refused(url, timeout):
        raise OSError("connection refused")

    assert tv_capture.cdp_alive("http://x", opener=refused) is False


class _closing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_launch_chrome_starts_headless_on_the_bot_profile_and_waits():
    started, answers = [], iter([False, True])
    tv_capture.launch_chrome(
        popen=lambda argv, **kw: started.append(argv),
        alive=lambda url: next(answers),
        sleep=lambda seconds: None,
    )
    argv = started[0]
    assert "--headless=new" in argv
    assert f"--user-data-dir={tv_capture.TV_SHOT_PROFILE}" in argv
    # The whole browser sits behind this port; it never leaves loopback.
    assert "--remote-debugging-address=127.0.0.1" in argv


def test_launch_chrome_gives_up_and_says_so():
    with pytest.raises(tv_capture.Unavailable, match="would not start"):
        tv_capture.launch_chrome(
            popen=lambda argv, **kw: None, alive=lambda url: False, sleep=lambda seconds: None
        )


def test_playwright_browser_attaches_over_cdp(monkeypatch):
    attached = {}

    class FakeChromium:
        def connect_over_cdp(self, url):
            attached["url"] = url
            return "browser"

    class FakePlay:
        chromium = FakeChromium()

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: types.SimpleNamespace(start=lambda: FakePlay())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    assert tv_capture._playwright_browser("http://127.0.0.1:9222") == "browser"
    assert attached["url"] == "http://127.0.0.1:9222"


def test_browser_reuses_a_running_chrome_and_caches_it():
    launched, connected = [], []
    shooter = tv_capture.Shooter(
        connect=lambda url: connected.append(url) or "browser",
        alive=lambda url: True,
        launch=lambda: launched.append(True),
    )
    assert shooter.browser() == "browser"
    assert shooter.browser() == "browser"
    assert launched == []  # the user's own headful window survives
    assert len(connected) == 1  # and the attach happens once


def test_browser_starts_chrome_when_nothing_answers():
    launched = []
    shooter = tv_capture.Shooter(
        connect=lambda url: "browser", alive=lambda url: False, launch=lambda: launched.append(True)
    )
    assert shooter.browser() == "browser"
    assert launched == [True]


# --- the shot itself -------------------------------------------------------


def test_shot_crops_the_pane_already_in_the_grid():
    page = FakePage(lambda url: row_of(["FARTCOINUSDT.P", "ARBUSDT.P", "NEARUSDT.P"]))
    assert shooter_for(page).shot("ARBUSDT.P") == b"PNG"
    # Probe first, then the window that makes a pane 760x460.
    assert page.viewports[0] == (2000, 1200)
    assert page.viewports[1] == (tv_capture.viewport((2000, 1200), (377.0, 1119.0), (3, 1)))
    assert page.closed == [True]


def test_shot_reads_a_pane_with_no_heading_without_dying():
    page = FakePage(
        lambda url: [FakePane(None, {"x": 0, "y": 0, "width": 377.0, "height": 1119.0})]
    )
    with pytest.raises(tv_capture.Unavailable):
        shooter_for(page).shot("SOLUSDT.P")


def test_shot_drives_a_pane_to_a_symbol_the_grid_does_not_hold():
    grid = ["FARTCOINUSDT.P", "ARBUSDT.P"]

    def panes_for(url):
        return row_of(["SOLUSDT.P", "ARBUSDT.P"] if "symbol=" in url else grid)

    page = FakePage(panes_for)
    assert shooter_for(page).shot("SOLUSDT.P") == b"PNG"


def test_shot_refuses_when_tradingview_will_not_show_the_symbol():
    page = FakePage(lambda url: row_of(["FARTCOINUSDT.P", "ARBUSDT.P"]))
    with pytest.raises(tv_capture.Unavailable, match="would not show NOPEUSDT.P"):
        shooter_for(page).shot("NOPEUSDT.P")
    assert page.closed == [True]  # the throwaway tab goes either way


def test_shot_refuses_rather_than_hand_back_the_wrong_symbol():
    """The resize reflows the grid; the pane must still be the one picked."""
    seen = {"n": 0}

    def panes_for(url):
        seen["n"] += 1
        # Third read is after the resize: the panes have swapped round.
        order = ["ARBUSDT.P", "SOLUSDT.P"] if seen["n"] > 2 else ["SOLUSDT.P", "ARBUSDT.P"]
        return row_of(order)

    page = FakePage(panes_for)
    with pytest.raises(tv_capture.Unavailable, match="moved while it was being shot"):
        shooter_for(page).shot("SOLUSDT.P")


def test_shot_refuses_when_the_profile_is_signed_out():
    page = FakePage(lambda url: row_of(["SOLUSDT.P"]))
    with pytest.raises(tv_capture.Unavailable, match="signed out"):
        shooter_for(page, names=("cookie_consent",)).shot("SOLUSDT.P")
    assert page.closed == []  # no tab was ever opened


# --- the door --------------------------------------------------------------


class FakeShooter:
    def __init__(self, answer=b"PNG"):
        self.answer, self.asked = answer, []

    def shot(self, symbol):
        self.asked.append(symbol)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.headers["Content-Type"], response.read()


def test_the_service_answers_a_png_on_the_secret_path():
    shooter = FakeShooter()
    httpd = tv_capture.serve(shooter=shooter)
    try:
        status, kind, body = get(httpd.server_address[1], "/shot/s3cret?symbol=SOLUSDT.P")
    finally:
        httpd.shutdown()
    assert (status, kind, body) == (200, "image/png", b"PNG")
    assert shooter.asked == ["SOLUSDT.P"]


def test_the_service_hangs_up_off_the_secret_path():
    shooter = FakeShooter()
    httpd = tv_capture.serve(shooter=shooter)
    try:
        port = httpd.server_address[1]
        # Not an HTTP error: the connection dies without a single byte.
        with pytest.raises((ConnectionError, urllib.error.URLError)):
            get(port, "/shot/wrong?symbol=SOLUSDT.P")
        with pytest.raises((ConnectionError, urllib.error.URLError)):
            get(port, "/json/version")  # never a way to the DevTools port
    finally:
        httpd.shutdown()
    assert shooter.asked == []


def test_the_service_turns_a_browser_failure_into_a_reason():
    shooter = FakeShooter(tv_capture.Unavailable("the TradingView profile is signed out"))
    httpd = tv_capture.serve(shooter=shooter)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as refusal:
            get(port, "/shot/s3cret?symbol=SOLUSDT.P")
    finally:
        httpd.shutdown()
    assert refusal.value.code == 503
    assert refusal.value.read() == b"the TradingView profile is signed out"


def test_the_service_will_not_take_a_non_symbol():
    httpd = tv_capture.serve(shooter=FakeShooter())
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as refusal:
            get(port, "/shot/s3cret?symbol=../../etc/passwd")
    finally:
        httpd.shutdown()
    assert refusal.value.code == 400


def test_serve_without_a_shooter_builds_its_own():
    httpd = tv_capture.serve()
    httpd.shutdown()


def test_main_refuses_to_run_unconfigured(monkeypatch):
    monkeypatch.setattr(tv_capture, "TV_SHOT_SECRET", "")
    assert tv_capture.main() == 1


def test_main_serves_until_it_is_killed(monkeypatch):
    served = []
    monkeypatch.setattr(tv_capture, "serve", lambda: served.append(True))
    waited = []

    class Once:
        def wait(self):
            waited.append(True)

    assert tv_capture.main(forever=Once) == 0
    assert served == [True] and waited == [True]
