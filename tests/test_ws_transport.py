"""WebSocketTransport must never send concurrently.

Regression: hot-path sends were fire-and-forget, so multiple ws.send_text()
coroutines ran at once. permessage-deflate keeps one stateful zlib compressor per
connection, so interleaved sends corrupted the stream and the browser reported
"Error -3 while decompressing data: incorrect header check".
"""

import asyncio
import threading
import time

import pytest

from tui_gateway.transport import WebSocketTransport


class RecordingWS:
    """Fails loudly if send_text is ever re-entered, like a real deflate stream would."""

    def __init__(self, delay=0.001):
        self.sent = []
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.overlaps = 0

    async def send_text(self, message):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight > 1:
            self.overlaps += 1
        try:
            await asyncio.sleep(self.delay)   # yield, so overlap is possible
            self.sent.append(message)
        finally:
            self.in_flight -= 1


@pytest.fixture()
def loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    # Wait for the loop to actually be running before handing it out.
    for _ in range(200):
        if loop.is_running():
            break
        time.sleep(0.005)
    yield loop

    # Cancel pending drain tasks before tearing the loop down, or they raise
    # "Event loop is closed" during GC.
    def _cancel_all():
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.call_soon_threadsafe(_cancel_all)
    time.sleep(0.05)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def _make_transport(loop, ws):
    box = {}
    done = threading.Event()

    def build():
        box["t"] = WebSocketTransport(ws)
        done.set()

    loop.call_soon_threadsafe(build)
    assert done.wait(timeout=5)
    return box["t"]


def _drain_settle(ws, expected, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(ws.sent) >= expected:
            return
        time.sleep(0.01)


class TestSerialization:
    def test_hot_path_sends_never_overlap(self, loop_thread):
        ws = RecordingWS()
        transport = _make_transport(loop_thread, ws)

        for i in range(200):
            transport.send(f"delta-{i}", flush=False)

        _drain_settle(ws, 200)
        assert ws.overlaps == 0, "ws.send_text was re-entered — deflate stream would corrupt"
        assert ws.max_in_flight == 1
        assert len(ws.sent) == 200

    def test_order_is_preserved(self, loop_thread):
        ws = RecordingWS()
        transport = _make_transport(loop_thread, ws)

        for i in range(100):
            transport.send(f"delta-{i}", flush=False)

        _drain_settle(ws, 100)
        assert ws.sent == [f"delta-{i}" for i in range(100)]

    def test_concurrent_producer_threads_stay_serialized(self, loop_thread):
        """Streaming deltas and status events come from different threads."""
        ws = RecordingWS()
        transport = _make_transport(loop_thread, ws)

        def produce(tag):
            for i in range(50):
                transport.send(f"{tag}-{i}", flush=False)

        threads = [threading.Thread(target=produce, args=(t,)) for t in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        _drain_settle(ws, 150)
        assert ws.overlaps == 0
        assert len(ws.sent) == 150

    def test_flush_waits_for_delivery(self, loop_thread):
        ws = RecordingWS(delay=0.02)
        transport = _make_transport(loop_thread, ws)

        transport.send("important", flush=True)
        # flush=True must not return before the frame is actually on the wire.
        assert "important" in ws.sent

    def test_mixed_flush_and_hot_keep_order(self, loop_thread):
        ws = RecordingWS()
        transport = _make_transport(loop_thread, ws)

        transport.send("a", flush=False)
        transport.send("b", flush=False)
        transport.send("c", flush=True)

        assert ws.sent == ["a", "b", "c"]

    def test_send_without_loop_is_noop(self):
        transport = WebSocketTransport.__new__(WebSocketTransport)
        transport._loop = None
        transport._queue = None
        transport._ws = None
        transport.send("x")   # must not raise


class TestNoDeadlockOnEventLoop:
    """flush=True from the loop thread must not wait on the drain task."""

    def test_on_loop_flush_does_not_block(self, loop_thread):
        ws = RecordingWS(delay=0.01)
        transport = _make_transport(loop_thread, ws)
        result = {}

        def call_from_loop():
            start = time.time()
            transport.send("from-loop", flush=True)   # would deadlock if it waited
            result["elapsed"] = time.time() - start
            result["done"] = True

        loop_thread.call_soon_threadsafe(call_from_loop)

        deadline = time.time() + 5
        while time.time() < deadline and not result.get("done"):
            time.sleep(0.01)

        assert result.get("done"), "send() from the event loop thread blocked"
        assert result["elapsed"] < 1.0
        _drain_settle(ws, 1)
        assert ws.sent == ["from-loop"]
