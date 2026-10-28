"""Deterministic, offline adapter for exercising composition; this is not an LLM."""

from collections.abc import AsyncGenerator

from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.service import MODELS
from my_own_agent.llm.types import (
    DataModel,
    Message,
    ModelDone,
    ModelEvent,
    ModelRequest,
    ModelSpec,
    TextDelta,
    ToolCall,
)


class DemoAdapter:
    def validate(self, model: ModelSpec) -> None:
        DataModel.model_validate(model.options)

    async def stream(self, model: ModelSpec, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        if request.messages[-1].role == "tool":
            text = f"Offline demo: current_time returned {request.messages[-1].content}"
            yield TextDelta(text)
            yield ModelDone(
                Message(role="assistant", content=text, model_alias=request.model_alias), "stop"
            )
        else:
            yield ModelDone(
                Message(
                    role="assistant",
                    model_alias=request.model_alias,
                    tool_calls=(
                        ToolCall(
                            id=f"demo_{len(request.messages)}", name="current_time", arguments="{}"
                        ),
                    ),
                ),
                "tool_calls",
            )


class DemoPlugin(BasePlugin):
    name = "demo"
    requires = ("models",)

    async def start(self, ctx: PluginContext) -> None:
        ctx.defer(ctx.services.get(MODELS).register("demo", DemoAdapter()))


def create_plugin(config: dict[str, JsonValue]) -> DemoPlugin:
    DataModel.model_validate(config)
    return DemoPlugin()
