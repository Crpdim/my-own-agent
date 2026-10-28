"""Serializable messages and provider-neutral streaming events."""

from dataclasses import dataclass
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, field_validator

from my_own_agent.core.clock import get_timezone


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(DataModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str  # Preserve raw JSON; validate only before tool execution.


class Message(DataModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    thinking: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False
    model_alias: str | None = None
    submitted_at: AwareDatetime | None = None  # User submission metadata; unknown for old history.


class ToolDefinition(DataModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, JsonValue]


class ModelSpec(DataModel):
    adapter: str = Field(min_length=1)
    model: str = Field(min_length=1)
    options: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeState(DataModel):
    """Host-observed request time and the current task's original submission time."""

    current_time: AwareDatetime
    timezone: str
    user_submitted_at: AwareDatetime | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_timezone(value)
        return value


class ModelRequest(DataModel):
    model_alias: str
    system: str = ""
    messages: tuple[Message, ...] = ()
    tools: tuple[ToolDefinition, ...] = ()
    runtime: RuntimeState | None = None


class Usage(DataModel):
    input_tokens: int = 0  # Total prompt tokens, INCLUDING cached input.
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0  # Subset of output_tokens; do not add again.


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True)
class UsageEvent:
    usage: Usage


type FinishReason = Literal["stop", "tool_calls", "length", "content_filter"]


@dataclass(frozen=True)
class ModelDone:
    message: Message
    reason: FinishReason


@dataclass(frozen=True)
class ModelError:
    code: str
    message: str
    retryable: bool = False


type ModelEvent = TextDelta | ThinkingDelta | ToolCallDelta | UsageEvent | ModelDone | ModelError
