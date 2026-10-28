import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator, ValidationError
from pydantic import JsonValue

from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import Message, ToolCall, ToolDefinition

type ToolHandler = Callable[[dict[str, JsonValue]], Awaitable[str]]


class ToolExecutionError(Exception):
    """An expected failure whose message is safe and useful to return to the model."""


@dataclass(frozen=True)
class Tool:
    definition: ToolDefinition
    execute: ToolHandler
    timeout_seconds: float = 30


class ToolService:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Callable[[], None]:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        Draft202012Validator.check_schema(tool.definition.parameters)
        if tool.definition.parameters.get("type") != "object":
            raise ValueError("Tool parameters must be an object schema")
        if tool.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive")
        self._tools[name] = tool

        def remove() -> None:
            if self._tools.get(name) is tool:
                del self._tools[name]

        return remove

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition.model_copy(deep=True) for tool in self._tools.values())

    async def execute(self, call: ToolCall) -> Message:
        try:
            if call.name not in self._tools:
                raise ValueError(f"Unknown tool: {call.name}")
            tool = self._tools[call.name]
            arguments = json.loads(call.arguments)
            Draft202012Validator(tool.definition.parameters).validate(arguments)
        except (ValueError, ValidationError) as error:
            detail = error.message if isinstance(error, ValidationError) else str(error)
            return self.error(call, f"Invalid tool request: {detail}")
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                result = await tool.execute(arguments)
            if not isinstance(result, str):
                raise TypeError("Tool handlers must return a string")
            return Message(role="tool", content=result, tool_call_id=call.id)
        except ToolExecutionError as error:
            return self.error(call, str(error))
        except TimeoutError:
            return self.error(call, "Tool timed out; verify effects before attempting again.")
        except Exception:
            return self.error(
                call, "Tool execution failed; verify effects before attempting again."
            )

    @staticmethod
    def error(call: ToolCall, message: str) -> Message:
        return Message(role="tool", content=message, tool_call_id=call.id, is_error=True)


TOOLS = ServiceKey[ToolService]("tools")
