"""Outbound delivery: a message nobody asked for.

Three callers wanted this and none could have it — the scheduler's `deliver`
field that nothing read, the `send_message` tool's missing gateway interface,
and flows. These cover the router and the scheduler's use of it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import patch

import pytest
from _fakes import FakeAdapter, run, wait_for

from gyrfalcon.gateway.config import GatewayConfig, PlatformConfig
from gyrfalcon.gateway.delivery import (
    DeliveryResult,
    DeliveryRouter,
    deliver_from_anywhere,
    get_router,
    parse_target,
    set_router,
)
from gyrfalcon.gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _no_leaked_router():
    set_router(None)
    yield
    set_router(None)


def adapter(secrets=None) -> FakeAdapter:
    return FakeAdapter(PlatformConfig.from_config("slack", {}), secrets)


class TestParseTarget:
    @pytest.mark.parametrize("raw,expected", [
        ("slack:C0123", ("slack", "C0123")),
        ("slack:#ops", ("slack", "ops")),
        ("slack:@U0456", ("slack", "U0456")),
        ("  slack : C1  ", ("slack", "C1")),
        ("SLACK:C1", ("slack", "C1")),
    ])
    def test_valid(self, raw, expected):
        assert parse_target(raw) == expected

    @pytest.mark.parametrize("raw", ["local", "LOCAL", "  local  ", "", None, "slack:", ":C1", "nonsense", ":"])
    def test_nothing_to_deliver_to(self, raw):
        assert parse_target(raw) is None

    @pytest.mark.parametrize("raw", ["local:anything", "LOCAL:C1"])
    def test_local_is_reserved_not_a_platform_name(self, raw):
        # Without this, `local:C1` parses as a platform called "local" and fails
        # later with a confusing "not connected" instead of being a no-op.
        assert parse_target(raw) is None


class TestRouter:
    def _router(self, adapters=None):
        async def build():
            return DeliveryRouter(adapters if adapters is not None else {"slack": adapter()},
                                  asyncio.get_running_loop())
        return build

    def test_it_sends_to_the_named_platform(self):
        async def scenario():
            a = adapter()
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            return await r.deliver("slack:C0123", "the nightly report"), a
        result, a = run(scenario())
        assert result.ok and bool(result) is True
        assert a.sent == [{"chat_id": "C0123", "content": "the nightly report", "reply_to": None}]

    def test_an_unconnected_platform_says_which_are(self):
        async def scenario():
            r = DeliveryRouter({"slack": adapter()}, asyncio.get_running_loop())
            return await r.deliver("telegram:12345", "hi")
        result = run(scenario())
        assert not result.ok and "telegram" in result.detail and "slack" in result.detail

    def test_no_platforms_at_all(self):
        async def scenario():
            r = DeliveryRouter({}, asyncio.get_running_loop())
            return await r.deliver("slack:C1", "hi")
        assert "none" in run(scenario()).detail

    def test_a_local_target_is_not_an_error_but_is_not_delivered(self):
        async def scenario():
            a = adapter()
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            return await r.deliver("local", "hi"), a
        result, a = run(scenario())
        assert not result.ok and a.sent == []

    def test_empty_text_is_refused(self):
        async def scenario():
            a = adapter()
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            return await r.deliver("slack:C1", "   "), a
        result, a = run(scenario())
        assert not result.ok and a.sent == []

    def test_secrets_are_scrubbed_on_the_way_out(self):
        async def scenario():
            a = adapter(secrets=["xoxb-real-token-value"])
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            await r.deliver("slack:C1", "the token is xoxb-real-token-value ok")
            return a
        text = run(scenario()).sent[0]["content"]
        assert "xoxb-real-token-value" not in text and "[redacted]" in text

    def test_a_send_failure_is_reported_not_raised(self):
        async def scenario():
            a = adapter()

            async def fail(*_a, **_k):
                from gyrfalcon.gateway.platforms.base import SendResult
                return SendResult(success=False, error="channel_not_found")
            a.send = fail
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            return await r.deliver("slack:C9", "hi")
        result = run(scenario())
        assert not result.ok and result.detail == "channel_not_found"

    def test_an_exploding_adapter_is_reported_by_type_only(self):
        async def scenario():
            a = adapter()

            async def boom(*_a, **_k):
                raise ConnectionResetError("failed talking to slack with xoxb-secret-1234567890")
            a.send = boom
            r = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            return await r.deliver("slack:C1", "hi")
        result = run(scenario())
        assert not result.ok and result.detail == "ConnectionResetError"
        assert "xoxb" not in result.detail


class TestFromAnotherThread:
    """The scheduler ticks in its own thread; adapters belong to the loop."""

    def test_a_background_thread_can_deliver(self):
        async def scenario():
            a = adapter()
            router = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            set_router(router)
            results = []

            def worker():
                results.append(deliver_from_anywhere("slack:C1", "from a scheduler thread"))

            t = threading.Thread(target=worker)
            t.start()
            await wait_for(lambda: results)   # the loop must stay free to serve it
            t.join()
            return results[0], a
        result, a = run(scenario())
        assert result.ok
        assert a.sent[0]["content"] == "from a scheduler thread"

    def test_with_no_gateway_running_it_says_so(self):
        # A job triggered by hand from the CLI has no gateway and no adapters.
        result = deliver_from_anywhere("slack:C1", "hi")
        assert not result.ok and "no gateway is running" in result.detail

    def test_a_stopped_loop_is_reported_rather_than_hanging(self):
        loop = asyncio.new_event_loop()
        router = DeliveryRouter({"slack": adapter()}, loop)
        try:
            result = router.deliver_threadsafe("slack:C1", "hi")
            assert not result.ok and "not running" in result.detail
        finally:
            loop.close()

    def test_a_slow_platform_times_out_instead_of_blocking_the_caller(self):
        async def scenario():
            a = adapter()

            async def forever(*_a, **_k):
                await asyncio.sleep(30)
            a.send = forever
            router = DeliveryRouter({"slack": a}, asyncio.get_running_loop())
            results = []

            def worker():
                results.append(router.deliver_threadsafe("slack:C1", "hi", timeout=0.15))

            t = threading.Thread(target=worker)
            t.start()
            await wait_for(lambda: results, timeout=5)
            t.join()
            return results[0]
        result = run(scenario())
        assert not result.ok and "timed out" in result.detail


class TestRunnerLifecycle:
    def test_the_router_exists_while_the_gateway_runs_and_not_after(self):
        async def scenario():
            config = GatewayConfig.from_dict({"platforms": {"slack": {"allow": {"users": ["U1"]}}}})
            made = []

            def loader(name):
                def build(cfg):
                    a = FakeAdapter(cfg)
                    made.append(a)
                    return a
                return build

            runner = GatewayRunner(config, session_db=type("DB", (), {"close": lambda self: None})(),
                                   run_scheduler=False, adapter_loader=loader)
            task = asyncio.create_task(runner.start())
            await wait_for(lambda: get_router() is not None)
            during = get_router()
            result = await during.deliver("slack:C1", "while running")
            await runner.stop()
            await asyncio.wait_for(task, 2)
            return during, result, made[0]
        during, result, a = run(scenario())
        assert during.describe_targets() == ["slack"]
        assert result.ok and a.sent[0]["content"] == "while running"
        assert get_router() is None, "and is gone once the gateway stops"

    def test_a_platform_that_failed_to_connect_is_not_a_target(self):
        async def scenario():
            config = GatewayConfig.from_dict({"platforms": {"slack": {"allow": {"users": ["U1"]}}}})

            def loader(name):
                def build(cfg):
                    a = FakeAdapter(cfg)

                    async def no():
                        return False
                    a.connect = no
                    return a
                return build

            runner = GatewayRunner(config, session_db=type("DB", (), {"close": lambda self: None})(),
                                   run_scheduler=False, adapter_loader=loader)
            task = asyncio.create_task(runner.start())
            await wait_for(lambda: get_router() is not None)
            result = await get_router().deliver("slack:C1", "hi")
            await runner.stop()
            await asyncio.wait_for(task, 2)
            return result
        assert not run(scenario()).ok


class TestSchedulerDelivery:
    """The `deliver` field has existed since before anything could read it."""

    @pytest.fixture()
    def scheduler_with(self, tmp_path):
        from gyrfalcon.scheduler import JobStore, Scheduler

        (tmp_path / "scheduler").mkdir(exist_ok=True)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            sched = Scheduler()
            sched._store = store
            yield sched, store, tmp_path

    def _job(self, store, **kw):
        return store.get(store.add({"prompt": "p", "schedule": "0 9 * * *", **kw}))

    def test_a_job_with_a_target_delivers_its_output(self, scheduler_with):
        sched, store, _ = scheduler_with
        job = self._job(store, name="Morning report", deliver="slack:C0123")
        sent = []
        with patch("gyrfalcon.gateway.delivery.deliver_from_anywhere",
                   lambda t, x: sent.append((t, x)) or DeliveryResult(True, "ts")):
            sched._deliver_output(job, "all systems normal")
        assert sent == [("slack:C0123", "*Morning report*\n\nall systems normal")]
        assert store.get(job["id"])["last_delivery_error"] is None

    def test_the_default_local_target_sends_nothing(self, scheduler_with):
        sched, store, _ = scheduler_with
        job = self._job(store)
        assert job["deliver"] == "local"
        sent = []
        with patch("gyrfalcon.gateway.delivery.deliver_from_anywhere",
                   lambda t, x: sent.append(t) or DeliveryResult(True)):
            sched._deliver_output(job, "output")
        assert sent == []

    def test_a_failed_delivery_is_recorded_and_does_not_fail_the_job(self, scheduler_with, caplog):
        sched, store, _ = scheduler_with
        job = self._job(store, deliver="slack:C1")
        with patch("gyrfalcon.gateway.delivery.deliver_from_anywhere",
                   lambda t, x: DeliveryResult(False, "channel_not_found")):
            with caplog.at_level(logging.WARNING):
                sched._deliver_output(job, "output")   # must not raise
        assert store.get(job["id"])["last_delivery_error"] == "channel_not_found"
        assert "delivery to slack:C1 failed" in caplog.text

    def test_a_successful_delivery_clears_a_previous_error(self, scheduler_with):
        sched, store, _ = scheduler_with
        job = self._job(store, deliver="slack:C1")
        store.update(job["id"], {"last_delivery_error": "channel_not_found"})
        with patch("gyrfalcon.gateway.delivery.deliver_from_anywhere", lambda t, x: DeliveryResult(True)):
            sched._deliver_output(store.get(job["id"]), "output")
        assert store.get(job["id"])["last_delivery_error"] is None

    def test_a_completed_run_delivers(self, scheduler_with):
        """The wiring, not just the helper: _execute_job must call it."""
        sched, store, _ = scheduler_with
        job = self._job(store, deliver="slack:C1")
        sent = []
        with patch.object(type(sched), "_run_prompt", lambda self, j: "the answer"), \
             patch("gyrfalcon.gateway.delivery.deliver_from_anywhere",
                   lambda t, x: sent.append((t, x)) or DeliveryResult(True)):
            sched._execute_job(store.get(job["id"]))
        assert len(sent) == 1 and sent[0][0] == "slack:C1" and "the answer" in sent[0][1]

    def test_a_failed_job_delivers_nothing(self, scheduler_with):
        sched, store, _ = scheduler_with
        job = self._job(store, deliver="slack:C1")
        sent = []

        def boom(self, j):
            raise RuntimeError("job failed")

        with patch.object(type(sched), "_run_prompt", boom), \
             patch("gyrfalcon.gateway.delivery.deliver_from_anywhere",
                   lambda t, x: sent.append(t) or DeliveryResult(True)):
            sched._execute_job(store.get(job["id"]))
        assert sent == []
        assert store.get(job["id"])["last_status"] == "error"

    def test_delivery_without_a_gateway_is_honest(self, scheduler_with, caplog):
        # The scheduler only ticks inside the gateway, but a job can be run by
        # hand from the CLI, where there are no adapters at all.
        sched, store, _ = scheduler_with
        job = self._job(store, deliver="slack:C1")
        with caplog.at_level(logging.WARNING):
            sched._deliver_output(job, "output")
        assert "no gateway is running" in store.get(job["id"])["last_delivery_error"]
