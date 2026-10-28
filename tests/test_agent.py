import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from my_own_agent.agent.loop import AGENT, Agent, ToolFinished, ToolStarted, TurnDone
from my_own_agent.config import create_application, load_config
from my_own_agent.core.events import EventBus
from my_own_agent.llm.service import ModelService
from my_own_agent.llm.types import (
    Message,
    ModelDone,
    ModelError,
    ModelSpec,
    TextDelta,
    ToolCall,
    ToolDefinition,
)
from my_own_agent.plugins.memory import MemoryStore
from my_own_agent.tools.service import Tool, ToolService


class ScriptedAdapter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.closed = 0

    def validate(self, model):
        pass

    async def stream(self, model, request):
        self.requests.append(request)
        try:
            for event in next(self.responses):
                yield event
        finally:
            self.closed += 1


def call(identifier="one", arguments='{"value": 2}', name="double"):
    return ToolCall(id=identifier, name=name, arguments=arguments)


def request_tools(*calls):
    return ModelDone(Message(role="assistant", tool_calls=calls), "tool_calls")


def make_agent(responses, max_steps=8, handler=None):
    adapter = ScriptedAdapter(responses)
    models = ModelService({"test": ModelSpec(adapter="custom", model="test")})
    models.register("custom", adapter)
    tools = ToolService()
    executions = []

    async def double(args):
        executions.append(args["value"])
        return str(args["value"] * 2)

    tools.register(
        Tool(
            ToolDefinition(
                name="double",
                description="Double a number",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            handler or double,
        )
    )
    sessions = MemoryStore()
    agent = Agent(models, tools, sessions, EventBus(), "test", "system", max_steps)
    return agent, adapter, sessions, executions


async def test_complete_tool_cycle_and_second_user_turn():
    answer = ModelDone(Message(role="assistant", content="4"), "stop")
    agent, adapter, store, executions = make_agent(
        [
            [request_tools(call())],
            [TextDelta("4"), answer],
            [answer],
        ]
    )
    events = [event async for event in agent.run("double two", "a")]
    assert [type(event) for event in events] == [
        ModelDone,
        ToolStarted,
        ToolFinished,
        TextDelta,
        ModelDone,
        TurnDone,
    ]
    assert events[-2] == answer
    assert executions == [2]
    assert adapter.requests[1].messages[-1].content == "4"
    assert [message.role for message in await store.load("a")] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    _ = [event async for event in agent.run("again", "a")]
    assert len(adapter.requests[-1].messages) == 5
    assert await store.load("b") == []
    assert adapter.closed == 3


@pytest.mark.parametrize(
    "arguments,name",
    [
        ("{", "double"),
        ('{"value":"two"}', "double"),
        ('{"value":2,"extra":true}', "double"),
        ("[]", "double"),
        ("{}", "missing"),
    ],
)
async def test_invalid_tool_requests_are_returned_without_execution(arguments, name):
    agent, adapter, store, executions = make_agent(
        [
            [request_tools(call(arguments=arguments, name=name))],
            [ModelDone(Message(role="assistant", content="cannot run"), "stop")],
        ]
    )
    events = [event async for event in agent.run("test")]
    result = next(event.result for event in events if isinstance(event, ToolFinished))
    assert result.is_error and result.tool_call_id == "one"
    assert adapter.requests[1].messages[-1] == result
    assert executions == []


async def test_max_steps_leaves_paired_tool_history():
    agent, adapter, store, executions = make_agent([[request_tools(call())]], max_steps=1)
    events = [event async for event in agent.run("test")]
    assert events[-1].code == "step_limit"
    assert (await store.load("default"))[-1].role == "tool"
    assert executions == [2]


@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_truncated_tool_call_never_executes(reason):
    agent, _, store, executions = make_agent(
        [
            [ModelDone(Message(role="assistant", tool_calls=(call(),)), reason)],
        ]
    )
    events = [event async for event in agent.run("test")]
    assert events[-1].code == reason
    assert executions == []
    assert len(await store.load("default")) == 1


async def test_error_after_partial_text_is_not_committed_or_retried():
    agent, adapter, store, _ = make_agent(
        [
            [TextDelta("partial"), ModelError("connection", "failed", True)],
        ]
    )
    events = [event async for event in agent.run("test")]
    assert events[-1].code == "connection"
    assert len(adapter.requests) == 1
    assert [message.role for message in await store.load("default")] == ["user"]


@pytest.mark.parametrize("close_at", [ModelDone, ToolStarted])
async def test_early_consumer_close_pairs_all_pending_calls(close_at):
    agent, adapter, store, executions = make_agent([[request_tools(call(), call("two"))]])
    async with aclosing(agent.run("test")) as stream:
        async for event in stream:
            if isinstance(event, close_at):
                break
    history = await store.load("default")
    assert [message.tool_call_id for message in history[-2:]] == ["one", "two"]
    assert all(message.is_error for message in history[-2:])
    assert executions == []
    assert adapter.closed == 1


async def test_early_close_during_model_stream_closes_adapter_without_recording_partial():
    agent, adapter, store, executions = make_agent([[TextDelta("partial"), request_tools(call())]])
    async with aclosing(agent.run("test")) as stream:
        assert await anext(stream) == TextDelta("partial")
    assert adapter.closed == 1
    assert executions == []
    assert [message.role for message in await store.load("default")] == ["user"]


@pytest.mark.parametrize(
    "response,code",
    [
        ([], "incomplete_stream"),
        ([ModelDone(Message(role="user", content="wrong role"), "stop")], "invalid_response"),
        ([ModelDone(Message(role="assistant"), "tool_calls")], "invalid_response"),
        ([ModelDone(Message(role="assistant", tool_calls=(call(),)), "stop")], "invalid_response"),
        ([request_tools(call(), call())], "invalid_response"),
    ],
)
async def test_invalid_model_response_is_not_recorded_or_executed(response, code):
    agent, adapter, store, executions = make_agent([response])
    events = [event async for event in agent.run("test")]
    assert len(events) == 1
    assert isinstance(events[0], ModelError) and events[0].code == code
    assert executions == []
    assert adapter.closed == 1
    assert [message.role for message in await store.load("default")] == ["user"]


async def test_cancellation_during_tool_preserves_completed_results():
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

    agent, _, store, _ = make_agent(
        [
            [request_tools(call(), call("two", '{"value": 3}'))],
        ],
        handler=handler,
    )

    async def consume():
        async with aclosing(agent.run("test")) as stream:
            return [event async for event in stream]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    history = await store.load("default")
    assert history[-2].content == "4" and not history[-2].is_error
    assert history[-1].tool_call_id == "two" and history[-1].is_error
    assert cancelled == [True]


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("saving_at", ["assistant", "first_result", "last_result"])
async def test_cancellation_during_tool_history_save_preserves_pairs_and_results(
    committed, saving_at
):
    saving = asyncio.Event()
    target_length = {"assistant": 2, "first_result": 3, "last_result": 4}[saving_at]

    class DelayedStore(MemoryStore):
        paused = False

        async def save(self, session_id, messages):
            if len(messages) == target_length and not self.paused:
                self.paused = True
                if committed:
                    await super().save(session_id, messages)
                saving.set()
                await asyncio.Event().wait()
            await super().save(session_id, messages)

    agent, adapter, _, executions = make_agent([[request_tools(call(), call("two"))]])
    store = DelayedStore()
    agent.sessions = store
    events = []

    async def consume():
        async with aclosing(agent.run("test")) as stream:
            async for event in stream:
                events.append(event)

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(2):
            await saving.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        history = await store.load("default")
        assert [message.role for message in history] == ["user", "assistant", "tool", "tool"]
        assert [message.tool_call_id for message in history[-2:]] == ["one", "two"]
        completed = {"assistant": 0, "first_result": 1, "last_result": 2}[saving_at]
        results = history[-2:]
        assert all(
            not message.is_error and message.content == "4" for message in results[:completed]
        )
        assert all(
            message.is_error and "unconfirmed" in message.content for message in results[completed:]
        )
        assert executions == [2] * completed
        assert not any(isinstance(event, TurnDone) for event in events)
        if saving_at == "assistant":
            assert events == []
        assert adapter.closed == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancellation_waits_for_tool_history_repair():
    saving, repairing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedStore(MemoryStore):
        async def save(self, session_id, messages):
            if messages[-1].tool_calls:
                await super().save(session_id, messages)
                saving.set()
                await asyncio.Event().wait()
            if messages[-1].role == "tool":
                repairing.set()
                await release.wait()
            await super().save(session_id, messages)

    agent, adapter, _, executions = make_agent([[request_tools(call())]])
    store = DelayedStore()
    agent.sessions = store

    async def consume():
        async with aclosing(agent.run("test")) as stream:
            return [event async for event in stream]

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(2):
            await saving.wait()
            task.cancel()
            await repairing.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        history = await store.load("default")
        assert [message.role for message in history] == ["user", "assistant", "tool"]
        assert history[-1].tool_call_id == "one" and history[-1].is_error
        assert executions == [] and adapter.closed == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_offline_config_loads_a_different_adapter_without_core_changes():
    async with create_application(load_config(Path("examples/demo.toml"))) as app:
        events = [event async for event in app.services.get(AGENT).run("time")]
        assert isinstance(events[-1], TurnDone)
        assert "Offline demo" in events[-1].message.content


async def test_tool_timeout_and_runtime_errors_become_results():
    async def slow(args):
        await asyncio.Event().wait()

    definition = ToolDefinition(name="slow", description="test", parameters={"type": "object"})
    tools = ToolService()
    tools.register(Tool(definition, slow, timeout_seconds=0.01))
    result = await tools.execute(call(name="slow", arguments="{}"))
    assert result.is_error and "timed out" in result.content


async def test_duplicate_adapter_registration_is_rejected():
    models = ModelService({})
    adapter = ScriptedAdapter([])
    remove = models.register("custom", adapter)
    with pytest.raises(ValueError, match="already registered"):
        models.register("custom", adapter)
    remove()
    models.register("custom", adapter)
