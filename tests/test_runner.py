import asyncio
from pathlib import Path

import pytest

from my_own_agent.agent.loop import TurnDone
from my_own_agent.agent.runner import RUNNER, AgentRunner, JobEvent, JobFinished, JobStarted
from my_own_agent.config import create_application, load_config
from my_own_agent.llm.types import Message, ModelDone, ModelError, TextDelta
from tests.test_agent import call, make_agent, request_tools


def answer(text):
    return ModelDone(Message(role="assistant", content=text), "stop")


@pytest.fixture
async def runner_factory():
    runners = []

    def make(responses, *, handler=None, max_pending=32):
        agent, adapter, store, executions = make_agent(responses, handler=handler)
        runner = AgentRunner(agent, agent.events, max_pending)
        runner.start()
        runners.append(runner)
        return runner, agent, adapter, store, executions

    yield make
    for runner in runners:
        await runner.close(drain=False)


async def test_fifo_uses_history_at_execution_time_and_correlates_events(runner_factory):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(args):
        entered.set()
        await release.wait()
        return "4"

    runner, agent, adapter, store, _ = runner_factory(
        [
            [request_tools(call())],
            [answer("first answer")],
            [answer("second answer")],
            [answer("other")],
        ],
        handler=handler,
    )
    notifications = []

    async def observe(event):
        notifications.append(event)

    agent.events.subscribe("runner.event", observe)
    first = runner.submit("first", "a")
    async with asyncio.timeout(2):
        await entered.wait()
        second = runner.submit("second", "a")
        other = runner.submit("third", "b")
        assert first.state == "running" and second.state == "queued"
        assert [m.content for m in await store.load("a") if m.role == "user"] == ["first"]
        assert await store.load("b") == []
        release.set()
        await runner.close()
    assert [(await job.result()).state for job in (first, second, other)] == ["succeeded"] * 3
    assert [n.job for n in notifications if isinstance(n, JobStarted)] == [
        first.job,
        second.job,
        other.job,
    ]
    assert [n.job for n in notifications if isinstance(n, JobFinished)] == [
        first.job,
        second.job,
        other.job,
    ]
    assert {n.job.job_id for n in notifications if isinstance(n, JobEvent)} == {
        first.job.job_id,
        second.job.job_id,
        other.job.job_id,
    }
    assert [m.content for m in adapter.requests[2].messages] == [
        "first",
        "",
        "4",
        "first answer",
        "second",
    ]
    assert [m.content for m in adapter.requests[3].messages] == ["third"]
    assert adapter.closed == 4


async def test_pending_capacity_rejects_without_recording_input(runner_factory):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(args):
        entered.set()
        await release.wait()
        return "4"

    runner, _, _, store, _ = runner_factory(
        [[request_tools(call())], [answer("done")], [answer("next")]],
        handler=handler,
        max_pending=1,
    )
    runner.submit("first")
    async with asyncio.timeout(2):
        await entered.wait()
        pending = runner.submit("second")
        with pytest.raises(asyncio.QueueFull):
            runner.submit("rejected")
        with pytest.raises(ValueError, match="empty"):
            runner.submit("  ")
        release.set()
        await runner.close()
    assert (await pending.result()).state == "succeeded"
    assert [m.content for m in await store.load("default") if m.role == "user"] == [
        "first",
        "second",
    ]


async def test_model_failure_does_not_stop_next_job(runner_factory):
    runner, _, adapter, _, _ = runner_factory(
        [[TextDelta("partial"), ModelError("connection", "failed", True)], [answer("ok")]]
    )
    failed, following = runner.submit("first"), runner.submit("second")
    await runner.close()
    result = await failed.result()
    assert result.state == "failed" and result.error.code == "connection"
    assert (await following.result()).message.content == "ok"
    assert len(adapter.requests) == 2  # No automatic retry of the failed request.


@pytest.mark.parametrize("failure", ["exception", "no_terminal", "cleanup"])
async def test_runtime_failures_settle_receipt_and_worker_continues(
    runner_factory, monkeypatch, caplog, failure
):
    runner, agent, _, _, _ = runner_factory([])
    closed = []

    async def run(prompt, session_id, *, submitted_at=None):
        try:
            if prompt == "bad":
                if failure == "exception":
                    raise ValueError("private-prompt-must-not-leak")
                if failure == "no_terminal":
                    return
            yield TurnDone(Message(role="assistant", content=prompt))
        finally:
            closed.append(prompt)
            if prompt == "bad" and failure == "cleanup":
                raise ValueError("private-prompt-must-not-leak")

    monkeypatch.setattr(agent, "run", run)
    failed, following = runner.submit("bad"), runner.submit("good")
    await runner.close()
    result = await failed.result()
    assert result.state == "failed" and result.error is not None
    assert (await following.result()).state == "succeeded"
    assert closed == ["bad", "good"]
    assert "private-prompt-must-not-leak" not in caplog.text + repr(result)


async def test_result_waiter_cancellation_does_not_cancel_execution(runner_factory, monkeypatch):
    runner, agent, _, _, _ = runner_factory([])
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(prompt, session_id, *, submitted_at=None):
        entered.set()
        await release.wait()
        yield TurnDone(Message(role="assistant", content="done"))

    monkeypatch.setattr(agent, "run", run)
    job = runner.submit("test")
    async with asyncio.timeout(2):
        await entered.wait()
        waiter = asyncio.create_task(job.result())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert job.state == "running"
        release.set()
        assert (await job.result()).state == "succeeded"
        assert (await job.result()).state == "succeeded"


async def test_receipt_and_next_job_wait_for_stream_cleanup(runner_factory, monkeypatch):
    runner, agent, _, _, _ = runner_factory([])
    cleaning, release = asyncio.Event(), asyncio.Event()
    started = []

    async def run(prompt, session_id, *, submitted_at=None):
        started.append(prompt)
        try:
            yield TurnDone(Message(role="assistant", content="done"))
        finally:
            if prompt == "first":
                cleaning.set()
                await release.wait()

    monkeypatch.setattr(agent, "run", run)
    first, second = runner.submit("first"), runner.submit("second")
    async with asyncio.timeout(2):
        await cleaning.wait()
        assert first.state == "running" and second.state == "queued"
        assert started == ["first"]
        release.set()
        await runner.close()
    assert first.state == second.state == "succeeded"


async def test_forced_close_pairs_tools_and_cancels_pending_without_recording_them(runner_factory):
    entered = asyncio.Event()
    cancelled = []

    async def handler(args):
        if args["value"] == 2:
            return "4"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    runner, _, adapter, store, _ = runner_factory(
        [[request_tools(call(), call("two", '{"value": 3}'))]], handler=handler
    )
    first = runner.submit("first")
    async with asyncio.timeout(2):
        await entered.wait()
        pending = runner.submit("pending")
        await runner.close(drain=False)
    assert first.state == pending.state == "cancelled"
    assert (await first.result()).state == (await pending.result()).state == "cancelled"
    history = await store.load("default")
    assert history[-2].content == "4" and not history[-2].is_error
    assert history[-1].tool_call_id == "two" and history[-1].is_error
    assert [m.content for m in history if m.role == "user"] == ["first"]
    assert cancelled == [True] and adapter.closed == 1
    with pytest.raises(RuntimeError, match="not accepting"):
        runner.submit("late")
    await runner.close()


async def test_forced_close_before_worker_starts_settles_all_jobs(runner_factory):
    runner, _, adapter, store, _ = runner_factory([])
    first, second = runner.submit("first"), runner.submit("second")
    await runner.close(drain=False)
    assert (await first.result()).state == (await second.result()).state == "cancelled"
    assert adapter.requests == [] and await store.load("default") == []


async def test_cancelling_graceful_close_forces_cleanup(runner_factory, monkeypatch):
    runner, agent, _, _, _ = runner_factory([])
    entered = asyncio.Event()
    closed = []

    async def run(prompt, session_id, *, submitted_at=None):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield TurnDone(Message(role="assistant"))
        finally:
            closed.append(True)

    monkeypatch.setattr(agent, "run", run)
    first, second = runner.submit("first"), runner.submit("second")
    async with asyncio.timeout(2):
        await entered.wait()
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="not accepting"):
            runner.submit("late")
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    assert first.state == second.state == "cancelled"
    assert closed == [True]


async def test_cleanup_error_during_forced_close_cannot_start_queued_job(
    runner_factory, monkeypatch
):
    runner, agent, _, _, _ = runner_factory([])
    entered = asyncio.Event()
    started = []

    async def run(prompt, session_id, *, submitted_at=None):
        started.append(prompt)
        if prompt == "first":
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                raise OSError("session cleanup failed")
        yield TurnDone(Message(role="assistant", content="done"))

    monkeypatch.setattr(agent, "run", run)
    first, second = runner.submit("first"), runner.submit("second")
    async with asyncio.timeout(2):
        await entered.wait()
        await runner.close(drain=False)
    assert first.state == "failed"  # The cleanup error remains visible.
    assert second.state == "cancelled"
    assert started == ["first"]


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_overlapping_close_does_not_interrupt_async_cleanup(
    runner_factory, monkeypatch, cancel_waiter
):
    runner, agent, _, _, _ = runner_factory([])
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def run(prompt, session_id, *, submitted_at=None):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield TurnDone(Message(role="assistant"))
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append(True)

    monkeypatch.setattr(agent, "run", run)
    first, second = runner.submit("first"), runner.submit("second")
    async with asyncio.timeout(2):
        await entered.wait()
        closing = asyncio.create_task(runner.close(drain=False))
        await cleaning.wait()
        if cancel_waiter:
            closing.cancel()
        another_close = asyncio.create_task(runner.close(drain=False))
        await asyncio.sleep(0)
        release.set()
        if cancel_waiter:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        await another_close
    assert cleaned == [True]
    assert first.state == second.state == "cancelled"


@pytest.mark.parametrize("drain", [False, True])
async def test_repeated_cancellation_of_close_waits_for_stream_cleanup(
    runner_factory, monkeypatch, drain
):
    runner, agent, _, _, _ = runner_factory([])
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    async def run(prompt, session_id, *, submitted_at=None):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield TurnDone(Message(role="assistant"))
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append(True)

    monkeypatch.setattr(agent, "run", run)
    first, second = runner.submit("first"), runner.submit("second")
    async with asyncio.timeout(2):
        await entered.wait()
        closing = asyncio.create_task(runner.close(drain=drain))
        try:
            await asyncio.sleep(0)
            closing.cancel()
            await cleaning.wait()
            # The first cancellation requests abort; later ones must not release ownership.
            for _ in range(2):
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
                assert cleaned == []
                assert first.state == "running" and second.state == "queued"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert cleaned == [True]
            assert first.state == second.state == "cancelled"
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)


async def test_plugin_drains_before_agent_dependencies_are_released():
    async with create_application(load_config(Path("examples/demo.toml"))) as app:
        runner = app.services.get(RUNNER)
        first, second = runner.submit("first"), runner.submit("second")
    assert (await first.result()).state == (await second.result()).state == "succeeded"


async def test_runner_lifecycle_is_explicit_and_single_use():
    agent, _, _, _ = make_agent([])
    with pytest.raises(ValueError, match="positive"):
        AgentRunner(agent, agent.events, max_pending=0)
    runner = AgentRunner(agent, agent.events)
    with pytest.raises(RuntimeError, match="not accepting"):
        runner.submit("test")
    runner.start()
    with pytest.raises(RuntimeError, match="once"):
        runner.start()
    await runner.close()
    await runner.close(drain=False)
    with pytest.raises(RuntimeError, match="once"):
        runner.start()
