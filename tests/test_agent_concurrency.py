import asyncio
from contextlib import aclosing

import pytest

from my_own_agent.agent.loop import AgentNotification, ToolStarted, TurnDone
from my_own_agent.llm.types import Message, ModelDone, ModelError, TextDelta
from my_own_agent.plugins.memory import MemoryStore
from my_own_agent.prompts.composer import Config, TextPromptComposer
from my_own_agent.prompts.service import PromptService
from tests.test_agent import call, make_agent, request_tools


def answer(text):
    return ModelDone(Message(role="assistant", content=text), "stop")


async def consume(agent, prompt, session_id, started=None):
    if started is not None:
        started.set()
    async with aclosing(agent.run(prompt, session_id)) as stream:
        return [event async for event in stream]


async def test_different_sessions_progress_while_a_model_stream_is_open():
    agent, adapter, store, _ = make_agent(
        [[TextDelta("partial"), answer("first answer")], [answer("other answer")]]
    )
    notifications = []

    async def observe(event):
        assert isinstance(event, AgentNotification)
        notifications.append(event)

    agent.events.subscribe("agent.event", observe)
    async with aclosing(agent.run("first", "a")) as first:
        assert await anext(first) == TextDelta("partial")
        async with asyncio.timeout(2):
            other = await consume(agent, "other", "b")
        assert isinstance(other[-1], TurnDone)
        assert [m.content for m in await store.load("b")] == ["other", "other answer"]
        assert [m.content for m in await store.load("a")] == ["first"]
        assert adapter.closed == 1  # The first model stream is still owned by its turn.
        _ = [event async for event in first]
    assert [m.content for m in await store.load("a")] == ["first", "first answer"]
    assert [
        (n.session_id, n.event.message.content)
        for n in notifications
        if isinstance(n.event, TurnDone)
    ] == [("b", "other answer"), ("a", "first answer")]
    assert adapter.closed == 2


@pytest.mark.parametrize("ending", ["complete", "close", "error"])
async def test_same_session_waits_for_the_whole_turn_and_reads_committed_history(ending):
    terminal = ModelError("connection", "failed") if ending == "error" else answer("first answer")
    agent, adapter, store, _ = make_agent(
        [[TextDelta("partial"), terminal], [answer("second answer")]]
    )
    started = asyncio.Event()
    async with aclosing(agent.run("first", "a")) as first:
        assert await anext(first) == TextDelta("partial")
        second = asyncio.create_task(consume(agent, "second", "a", started))
        try:
            async with asyncio.timeout(2):
                await started.wait()
                assert not second.done()
                assert len(adapter.requests) == 1
                assert [m.content for m in await store.load("a")] == ["first"]
                if ending == "close":
                    await first.aclose()
                else:
                    _ = [event async for event in first]
                await second
            expected = ["first", "first answer"] if ending == "complete" else ["first"]
            assert [m.content for m in adapter.requests[1].messages] == [*expected, "second"]
            assert [m.content for m in await store.load("a")] == [
                *expected,
                "second",
                "second answer",
            ]
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


async def test_cancelling_a_waiter_does_not_release_the_active_session():
    agent, adapter, store, _ = make_agent([[TextDelta("partial")], [answer("next answer")]])
    started, next_started = asyncio.Event(), asyncio.Event()
    tasks = []
    async with aclosing(agent.run("first", "a")) as first:
        await anext(first)
        try:
            async with asyncio.timeout(2):
                cancelled = asyncio.create_task(consume(agent, "cancelled", "a", started))
                tasks.append(cancelled)
                await started.wait()
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                following = asyncio.create_task(consume(agent, "next", "a", next_started))
                tasks.append(following)
                await next_started.wait()
                assert not following.done()
                assert len(adapter.requests) == 1
                assert [m.content for m in await store.load("a")] == ["first"]
                await first.aclose()
                await following
            assert [m.content for m in await store.load("a")] == ["first", "next", "next answer"]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_session_lock_survives_handoff_to_a_waiting_stream():
    agent, adapter, store, _ = make_agent(
        [
            [TextDelta("first partial")],
            [TextDelta("second partial"), answer("second answer")],
            [answer("third answer")],
        ]
    )
    waiting, third_started = asyncio.Event(), asyncio.Event()
    tasks = []
    async with (
        aclosing(agent.run("first", "a")) as first,
        aclosing(agent.run("second", "a")) as second,
    ):
        await anext(first)

        async def start_second():
            waiting.set()
            return await anext(second)

        try:
            async with asyncio.timeout(2):
                next_event = asyncio.create_task(start_second())
                tasks.append(next_event)
                await waiting.wait()
                assert not next_event.done()
                await first.aclose()
                assert await next_event == TextDelta("second partial")
                third = asyncio.create_task(consume(agent, "third", "a", third_started))
                tasks.append(third)
                await third_started.wait()
                assert not third.done()
                assert len(adapter.requests) == 2
                _ = [event async for event in second]
                await third
            assert [m.content for m in await store.load("a")] == [
                "first",
                "second",
                "second answer",
                "third",
                "third answer",
            ]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_prompt_preview_waits_only_for_its_session_and_sees_paired_tools():
    agent, adapter, store, executions = make_agent([[request_tools(call())], [answer("done")]])
    agent.prompts = PromptService(TextPromptComposer(Config()))
    started = asyncio.Event()

    async def preview_same():
        started.set()
        return await agent.inspect_prompt("preview", "a")

    async with aclosing(agent.run("first", "a")) as first:
        async for event in first:
            if isinstance(event, ToolStarted):
                break
        preview = asyncio.create_task(preview_same())
        try:
            async with asyncio.timeout(2):
                await started.wait()
                assert not preview.done()
                other = await agent.inspect_prompt("other preview", "b")
                assert [m.content for m in other.request.messages] == ["other preview"]
                assert await store.load("b") == []
                assert executions == [] and len(adapter.requests) == 1
                _ = [event async for event in first]
                prepared = await preview
            history = await store.load("a")
            assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
            assert prepared.request.messages[:-1] == tuple(history)
            assert prepared.request.messages[-1].content == "preview"
            assert executions == [2] and len(adapter.requests) == 2
        finally:
            preview.cancel()
            await asyncio.gather(preview, return_exceptions=True)


async def test_running_preview_blocks_only_turns_in_its_session(monkeypatch):
    agent, _, store, _ = make_agent([[answer("other answer")], [answer("same answer")]])
    agent.prompts = PromptService(TextPromptComposer(Config()))
    prepare = agent.prompts.prepare
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_prepare(request, *, capture=False):
        if request.messages[-1].content == "preview":
            entered.set()
            await release.wait()
        return await prepare(request, capture=capture)

    monkeypatch.setattr(agent.prompts, "prepare", delayed_prepare)
    preview = asyncio.create_task(agent.inspect_prompt("preview", "a"))
    tasks = [preview]
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            same = asyncio.create_task(consume(agent, "same", "a", started))
            tasks.append(same)
            await started.wait()
            assert not same.done() and await store.load("a") == []
            await consume(agent, "other", "b")
            assert not same.done() and await store.load("a") == []
            release.set()
            prepared = await preview
            await same
        assert [m.content for m in prepared.request.messages] == ["preview"]
        assert [m.content for m in await store.load("a")] == ["same", "same answer"]
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_turn_keeps_its_session_until_history_repair_finishes():
    entered, repairing, release, started = (asyncio.Event() for _ in range(4))

    async def handler(args):
        entered.set()
        await asyncio.Event().wait()

    class DelayedStore(MemoryStore):
        async def save(self, session_id, messages):
            if session_id == "a" and messages[-1].role == "tool":
                repairing.set()
                await release.wait()
            await super().save(session_id, messages)

    agent, adapter, _, _ = make_agent(
        [[request_tools(call())], [answer("other answer")], [answer("next answer")]],
        handler=handler,
    )
    agent.sessions = store = DelayedStore()
    first = asyncio.create_task(consume(agent, "first", "a"))
    tasks = [first]
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            following = asyncio.create_task(consume(agent, "next", "a", started))
            tasks.append(following)
            await started.wait()
            first.cancel()
            await repairing.wait()
            first.cancel()
            await consume(agent, "other", "b")
            assert not first.done() and not following.done()
            assert len(adapter.requests) == 2
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await following
        messages = adapter.requests[2].messages
        assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
        assert messages[2].is_error and messages[2].tool_call_id == "one"
        assert [m.content for m in await store.load("a") if m.role == "user"] == ["first", "next"]
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
