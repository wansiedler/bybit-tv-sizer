"""Tests for the TradingView webhook receiver: real ephemeral port, loopback."""

import asyncio
import urllib.error
import urllib.request

import pytest

import tv_alerts


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(tv_alerts, "TV_PORT", 0)  # ephemeral: no clash in CI


def test_enabled_follows_the_secret(monkeypatch):
    assert tv_alerts.enabled() is True
    monkeypatch.setattr(tv_alerts, "TV_WEBHOOK_SECRET", "")
    assert tv_alerts.enabled() is False


def post(port, path, body=b"CL broke 92"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


def test_webhook_queues_the_alert_text():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            status = await asyncio.to_thread(post, port, "/tv/s3cret")
            assert status == 200
            return await asyncio.wait_for(queue.get(), timeout=5)
        finally:
            httpd.shutdown()

    assert asyncio.run(run()) == "CL broke 92"


def test_webhook_refuses_a_wrong_secret():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                await asyncio.to_thread(post, port, "/tv/wrong")
            assert excinfo.value.code == 404
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_webhook_ignores_an_empty_body():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            assert await asyncio.to_thread(post, port, "/tv/s3cret", b"") == 200
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_webhook_caps_a_huge_body():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]
            await asyncio.to_thread(post, port, "/tv/s3cret", b"x" * 10_000)
            return await asyncio.wait_for(queue.get(), timeout=5)
        finally:
            httpd.shutdown()

    assert len(asyncio.run(run())) == tv_alerts.MAX_BODY


def test_get_shows_the_alive_page_only_on_the_secret_path():
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        httpd = tv_alerts.serve(asyncio.get_running_loop(), queue)
        try:
            port = httpd.server_address[1]

            def get(path):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=5
                    ) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as refusal:
                    return refusal.code, refusal.read()

            status, body = await asyncio.to_thread(get, "/tv/s3cret")
            assert status == 200
            assert b"lexx-relay" in body

            for path in ("/", "/tv/wrong", "/anything"):
                status, body = await asyncio.to_thread(get, path)
                assert status == 404
                assert "недоступно".encode() in body
            assert queue.empty()
        finally:
            httpd.shutdown()

    asyncio.run(run())


def test_request_logging_goes_to_the_logger(caplog):
    handler = tv_alerts._Handler.__new__(tv_alerts._Handler)

    with caplog.at_level("DEBUG", logger="relay.tv"):
        tv_alerts._Handler.log_message(handler, "%s hit", "/tv/x")

    assert "/tv/x hit" in caplog.text


# --------------------------------------------------------------------------- #
#  pump                                                                        #
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self):
        self.sent: list[str] = []
        self.spoken: list[str] = []
        self.fail_once = False

    async def send(self, text):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("telegram down")
        self.sent.append(text)

    async def speak(self, text):
        self.spoken.append(text)
        return True


def drive(queue_items, out):
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        for item in queue_items:
            queue.put_nowait(item)
        task = asyncio.create_task(tv_alerts.pump(queue, out.send, out.speak))
        await asyncio.sleep(0.05)  # let the pump drain the queue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_pump_announces_and_speaks():
    out = Recorder()

    drive(["CL broke 92"], out)

    assert out.sent == ["🔔 TV: CL broke 92"]
    assert out.spoken == ["CL broke 92"]


def test_pump_survives_a_failing_send(caplog):
    out = Recorder()
    out.fail_once = True

    with caplog.at_level("ERROR", logger="relay.tv"):
        drive(["first", "second"], out)

    assert out.sent == ["🔔 TV: second"]  # the first failed, the loop lived
    assert "could not announce" in caplog.text


def test_pump_lets_cancellation_through():
    class Cancelling:
        async def send(self, text):
            raise asyncio.CancelledError

        async def speak(self, text):
            return True

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait("x")
        with pytest.raises(asyncio.CancelledError):
            await tv_alerts.pump(queue, Cancelling().send, Cancelling().speak)

    asyncio.run(run())
