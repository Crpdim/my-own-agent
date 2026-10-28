"""OpenAI Chat Completions protocol adapter, including opt-in wire differences."""

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal, Self, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import Field, JsonValue, model_validator

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.service import MODELS
from my_own_agent.llm.types import (
    DataModel,
    FinishReason,
    Message,
    ModelDone,
    ModelError,
    ModelEvent,
    ModelRequest,
    ModelSpec,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
)


class ChatOptions(DataModel):
    base_url: str = Field(pattern=r"^https?://[^/]+")
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=120, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    temperature: float | None = Field(default=None, ge=0, le=2)
    include_usage: bool = True
    reasoning_field: str = "reasoning_content"
    replay_thinking: bool = False
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def protect_request(self) -> Self:
        reserved = {
            "model",
            "messages",
            "tools",
            "stream",
            "n",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
        }
        if reserved.intersection(self.extra_body):
            raise ValueError("extra_body cannot override core request fields")
        if self.reasoning_field in {"role", "content", "tool_calls", "tool_call_id"}:
            raise ValueError("reasoning_field cannot override a message field")
        return self


def serialize_messages(request: ModelRequest, options: ChatOptions) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        wire: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "assistant":
            if message.tool_calls:
                wire["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            # Replay all same-route assistant reasoning, including turns without calls.
            # Never send another model's private protocol state as native history.
            if options.replay_thinking and message.model_alias == request.model_alias:
                wire[options.reasoning_field] = message.thinking
        elif message.role == "tool":
            if not message.tool_call_id:
                raise ValueError("Tool results require tool_call_id")
            wire["tool_call_id"] = message.tool_call_id
        messages.append(wire)
    return messages


class OpenAIChatAdapter:
    def __init__(self, client_factory: Callable[..., AsyncOpenAI] = AsyncOpenAI) -> None:
        self._client_factory = client_factory

    def validate(self, model: ModelSpec) -> None:
        ChatOptions.model_validate(model.options)

    async def stream(self, model: ModelSpec, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        options = ChatOptions.model_validate(model.options)
        key = os.environ.get(options.api_key_env) if options.api_key_env else "local-no-key"
        if not key:
            yield ModelError(
                "missing_credential", f"Set environment variable {options.api_key_env}"
            )
            return
        body: dict[str, Any] = {
            "model": model.model,
            "messages": serialize_messages(request, options),
            "stream": True,
        }
        if request.tools:
            body["tools"] = [
                {"type": "function", "function": definition.model_dump()}
                for definition in request.tools
            ]
        if options.include_usage:
            body["stream_options"] = {"include_usage": True}
        if options.max_tokens is not None:
            body[options.token_limit_field] = options.max_tokens
        if options.temperature is not None:
            body["temperature"] = options.temperature
        if options.extra_body:
            body["extra_body"] = options.extra_body

        text: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        try:
            # Exactly one attempt: never automatically replay a partially consumed turn.
            async with asyncio.timeout(options.timeout_seconds):
                async with self._client_factory(
                    api_key=key,
                    base_url=options.base_url,
                    timeout=options.timeout_seconds,
                    max_retries=0,
                ) as client:
                    stream = await client.chat.completions.create(**body)
                    async with stream:
                        async for chunk in stream:
                            if chunk.usage is not None:
                                usage = chunk.usage
                                cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0)
                                reasoning = getattr(
                                    usage.completion_tokens_details, "reasoning_tokens", 0
                                )
                                yield UsageEvent(
                                    Usage(
                                        input_tokens=usage.prompt_tokens,
                                        output_tokens=usage.completion_tokens,
                                        cached_input_tokens=getattr(
                                            usage, "prompt_cache_hit_tokens", cached
                                        )
                                        or 0,
                                        reasoning_tokens=reasoning or 0,
                                    )
                                )
                            for choice in chunk.choices:
                                if choice.index != 0:
                                    raise ValueError("Only one completion choice is supported")
                                if finish_reason is not None:
                                    raise ValueError("Received content after finish_reason")
                                delta = choice.delta
                                content = delta.content or getattr(delta, "refusal", None)
                                if content:
                                    text.append(content)
                                    yield TextDelta(content)
                                thought = getattr(delta, options.reasoning_field, None)
                                if thought:
                                    thinking.append(thought)
                                    yield ThinkingDelta(thought)
                                for part in delta.tool_calls or ():
                                    if part.index < 0 or (part.type and part.type != "function"):
                                        raise ValueError("Unsupported tool call")
                                    call = calls.setdefault(
                                        part.index, {"id": "", "name": "", "arguments": ""}
                                    )
                                    function = part.function
                                    identifier = part.id or ""
                                    name = function.name or "" if function else ""
                                    arguments = function.arguments or "" if function else ""
                                    if identifier:
                                        if call["id"] and call["id"] != identifier:
                                            raise ValueError("Tool call ID changed within a stream")
                                        call["id"] = identifier
                                    if name:
                                        if call["name"] and call["name"] != name:
                                            raise ValueError("Tool name changed within a stream")
                                        call["name"] = name
                                    call["arguments"] += arguments
                                    yield ToolCallDelta(part.index, identifier, name, arguments)
                                if choice.finish_reason:
                                    finish_reason = choice.finish_reason
            if finish_reason is None:
                yield ModelError("incomplete_stream", "Stream ended without a finish reason", True)
                return
            if finish_reason not in ("stop", "tool_calls", "length", "content_filter"):
                yield ModelError("provider_finish", f"Unsupported finish reason: {finish_reason}")
                return
            tool_calls = tuple(ToolCall(**calls[index]) for index in sorted(calls))
            if len({call.id for call in tool_calls}) != len(tool_calls):
                raise ValueError("Duplicate tool call IDs")
            if finish_reason in {"stop", "tool_calls"}:
                if bool(tool_calls) != (finish_reason == "tool_calls"):
                    raise ValueError("Tool calls and finish reason disagree")
                if not text and not tool_calls:
                    yield ModelError(
                        "empty_response", "Model returned no answer or tool calls", True
                    )
                    return
            yield ModelDone(
                Message(
                    role="assistant",
                    content="".join(text),
                    thinking="".join(thinking),
                    tool_calls=tool_calls,
                    model_alias=request.model_alias,
                ),
                cast(FinishReason, finish_reason),  # Validated against supported reasons above.
            )
        except (APITimeoutError, TimeoutError):
            yield ModelError("timeout", "Model request timed out", True)
        except APIStatusError as error:
            status = error.status_code
            code = {401: "authentication", 403: "permission", 429: "rate_limit"}.get(
                status, "http_error"
            )
            # Provider error bodies may contain prompts or credentials; do not echo them.
            yield ModelError(
                code, f"Model endpoint returned HTTP {status}", status == 429 or status >= 500
            )
        except APIConnectionError:
            yield ModelError("connection", "Could not connect to the model endpoint", True)
        except (ValueError, TypeError, AttributeError):
            yield ModelError("invalid_response", "Model endpoint returned an invalid response")


class OpenAIChatPlugin(BasePlugin):
    name = "openai_chat"
    requires = ("models",)

    async def start(self, ctx: PluginContext) -> None:
        ctx.defer(ctx.services.get(MODELS).register("openai_chat", OpenAIChatAdapter()))


def create_plugin(config: dict[str, JsonValue]) -> OpenAIChatPlugin:
    DataModel.model_validate(config)
    return OpenAIChatPlugin()
