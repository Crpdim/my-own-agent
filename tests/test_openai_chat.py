import asyncio
import json
from contextlib import aclosing

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from my_own_agent.agent.loop import Agent, TurnDone
from my_own_agent.core.events import EventBus
from my_own_agent.llm.service import ModelService
from my_own_agent.llm.types import (
    Message,
    ModelDone,
    ModelError,
    ModelRequest,
    ModelSpec,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolDefinition,
    UsageEvent,
)
from my_own_agent.plugins.memory import MemoryStore
from my_own_agent.plugins.openai_chat import ChatOptions, OpenAIChatAdapter, serialize_messages
from my_own_agent.tools.service import Tool, ToolService


def chunk(delta=None, finish=None, usage=None):
    return {
        "id": "response_1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": []
        if delta is None
        else [
            {"index": 0, "delta": delta, "finish_reason": finish},
        ],
        "usage": usage,
    }


def response(chunks):
    content = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


def adapter_for(handler):
    clients = []

    def factory(**kwargs):
        client = AsyncOpenAI(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
        )
        clients.append(client)
        return client

    return OpenAIChatAdapter(factory), clients


def model(**options):
    return ModelSpec(
        adapter="openai_chat",
        model="configured-model",
        options={
            "base_url": "https://example.test/v1",
            **options,
        },
    )


def request():
    return ModelRequest(
        model_alias="test", system="system", messages=(Message(role="user", content="hi"),)
    )


async def test_real_sdk_assembles_interleaved_calls_and_final_usage():
    bodies = []

    def handler(req):
        assert req.url.path == "/v1/chat/completions"
        bodies.append(json.loads(req.content))
        return response(
            [
                chunk({"reasoning_content": "think"}),
                chunk({"content": "checking"}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "type": "function",
                                "function": {"name": "second", "arguments": "{"},
                            },
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {"name": "first", "arguments": '{"value":'},
                            },
                        ]
                    }
                ),
                chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "2}"}},
                            {"index": 1, "function": {"arguments": "}"}},
                        ]
                    }
                ),
                chunk({}, "tool_calls"),
                chunk(
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "prompt_cache_hit_tokens": 4,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    }
                ),
            ]
        )

    adapter, clients = adapter_for(handler)
    events = [event async for event in adapter.stream(model(), request())]
    assert isinstance(events[0], ThinkingDelta)
    assert any(isinstance(event, ToolCallDelta) for event in events)
    assert isinstance(events[-2], UsageEvent)
    assert events[-2].usage.cached_input_tokens == 4
    assert events[-2].usage.reasoning_tokens == 2
    done = events[-1]
    assert isinstance(done, ModelDone), events
    assert [call.id for call in done.message.tool_calls] == ["a", "b"]
    assert done.message.tool_calls[0].arguments == '{"value":2}'
    assert done.message.thinking == "think"
    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert clients[0].is_closed()


async def test_sdk_tool_loop_replays_deepseek_reasoning_across_user_turns():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            return response(
                [
                    chunk({"reasoning_content": "Need a tool."}),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "echo", "arguments": "{}"},
                                }
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            )
        return response(
            [
                chunk({"reasoning_content": "I have the result.", "content": "done"}),
                chunk({}, "stop"),
            ]
        )

    adapter, _ = adapter_for(handler)
    service = ModelService(
        {"test": model(replay_thinking=True, extra_body={"thinking": {"type": "enabled"}})}
    )
    service.register("openai_chat", adapter)
    tools = ToolService()

    async def echo(args):
        return "ok"

    tools.register(
        Tool(ToolDefinition(name="echo", description="echo", parameters={"type": "object"}), echo)
    )
    agent = Agent(service, tools, MemoryStore(), EventBus(), "test", "system")
    first = [event async for event in agent.run("hello")]
    second = [event async for event in agent.run("follow up")]
    assert isinstance(first[-1], TurnDone) and isinstance(second[-1], TurnDone)
    assert bodies[1]["messages"][-2]["reasoning_content"] == "Need a tool."
    assert bodies[1]["messages"][-1] == {"role": "tool", "content": "ok", "tool_call_id": "c1"}
    history = bodies[2]["messages"]
    assert history[-2]["reasoning_content"] == "I have the result."
    assert bodies[0]["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize(
    "status,code,retryable",
    [(401, "authentication", False), (429, "rate_limit", True), (500, "http_error", True)],
)
async def test_http_errors_are_normalized_without_retrying_or_exposing_body(
    status, code, retryable
):
    attempts = []

    def handler(req):
        attempts.append(req)
        return httpx.Response(status, json={"error": {"message": "secret-value", "type": "bad"}})

    adapter, clients = adapter_for(handler)
    events = [event async for event in adapter.stream(model(), request())]
    assert len(attempts) == 1
    assert events == [ModelError(code, f"Model endpoint returned HTTP {status}", retryable)]
    assert "secret-value" not in str(events)
    assert clients[0].is_closed()


async def test_missing_credential_never_creates_a_client(monkeypatch):
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)

    def forbidden(**kwargs):
        pytest.fail("No client should be created")

    events = [
        event
        async for event in OpenAIChatAdapter(forbidden).stream(
            model(api_key_env="TEST_MODEL_KEY"),
            request(),
        )
    ]
    assert isinstance(events[0], ModelError)
    assert events[0].code == "missing_credential"


async def test_provider_options_and_environment_key(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test-value")

    def handler(req):
        body = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer test-value"
        assert body["model"] == "configured-model"
        assert body["max_completion_tokens"] == 12
        assert "stream_options" not in body
        assert "tools" not in body
        return response([chunk({"content": "ok"}, "stop")])

    adapter, _ = adapter_for(handler)
    events = [
        event
        async for event in adapter.stream(
            model(
                api_key_env="TEST_MODEL_KEY",
                max_tokens=12,
                token_limit_field="max_completion_tokens",
                include_usage=False,
            ),
            request(),
        )
    ]
    assert isinstance(events[-1], ModelDone)


@pytest.mark.parametrize(
    "chunks,code",
    [
        ([chunk({"content": "partial"})], "incomplete_stream"),
        ([chunk({}, "stop")], "empty_response"),
        ([chunk({}, "tool_calls")], "invalid_response"),
        (
            [chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, "tool_calls")],
            "invalid_response",
        ),
    ],
)
async def test_incomplete_or_invalid_stream_does_not_look_successful(chunks, code):
    adapter, clients = adapter_for(lambda req: response(chunks))
    events = [event async for event in adapter.stream(model(), request())]
    assert events[-1].code == code
    assert not any(isinstance(event, ModelDone) for event in events)
    assert clients[0].is_closed()


async def test_connection_timeout_is_an_event():
    def handler(req):
        raise httpx.ReadTimeout("secret endpoint detail", request=req)

    adapter, _ = adapter_for(handler)
    events = [event async for event in adapter.stream(model(), request())]
    assert events == [ModelError("timeout", "Model request timed out", True)]


async def test_closing_consumer_closes_sdk_client():
    adapter, clients = adapter_for(
        lambda req: response([chunk({"content": "one"}), chunk({"content": "two"}, "stop")])
    )
    async with aclosing(adapter.stream(model(), request())) as stream:
        assert isinstance(await anext(stream), TextDelta)
    assert clients[0].is_closed()


async def test_cancellation_closes_sdk_client():
    entered = asyncio.Event()

    async def handler(req):
        entered.set()
        await asyncio.Event().wait()

    adapter, clients = adapter_for(handler)

    async def consume():
        return [event async for event in adapter.stream(model(), request())]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].is_closed()


def test_foreign_reasoning_is_not_replayed():
    req = ModelRequest(
        model_alias="new",
        messages=(
            Message(role="assistant", content="answer", thinking="private", model_alias="old"),
        ),
    )
    messages = serialize_messages(
        req, ChatOptions(base_url="https://example.test", replay_thinking=True)
    )
    assert "reasoning_content" not in messages[0]


@pytest.mark.parametrize("field", ["model", "messages", "tools", "stream", "n"])
def test_provider_options_cannot_replace_core_request_fields(field):
    with pytest.raises(ValidationError):
        ChatOptions(base_url="https://example.test", extra_body={field: "override"})
