"""Offline embedding example; copy this file into a project that installs my-own-agent."""

import asyncio
from collections.abc import AsyncGenerator

from pydantic import JsonValue

from my_own_agent.agent.runner import RUNNER
from my_own_agent.core.plugins import Application, BasePlugin, PluginContext
from my_own_agent.llm.service import MODELS
from my_own_agent.llm.types import (
    DataModel,
    Message,
    ModelDone,
    ModelEvent,
    ModelRequest,
    ModelSpec,
    ToolCall,
    ToolDefinition,
)
from my_own_agent.plugins.agent import AgentPlugin
from my_own_agent.plugins.agent import Config as AgentConfig
from my_own_agent.plugins.memory import MemoryPlugin
from my_own_agent.plugins.models import Config as ModelsConfig
from my_own_agent.plugins.models import ModelsPlugin
from my_own_agent.plugins.runner import Config as RunnerConfig
from my_own_agent.plugins.runner import RunnerPlugin
from my_own_agent.plugins.tools import ToolsPlugin
from my_own_agent.tools.service import TOOLS, Tool, ToolExecutionError


class ScriptedQueryAdapter:
    """A fixed tool round trip, not a language model or a real order system."""

    def validate(self, model: ModelSpec) -> None:
        DataModel.model_validate(model.options)

    async def stream(self, model: ModelSpec, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        if request.messages[-1].role == "user":
            call = ToolCall(id="lookup-1", name="lookup_order", arguments='{"order_id":"DEMO-001"}')
            yield ModelDone(Message(role="assistant", tool_calls=(call,)), "tool_calls")
        else:
            yield ModelDone(Message(role="assistant", content=request.messages[-1].content), "stop")


class QueryDemoPlugin(BasePlugin):
    name = "query_demo"
    requires = ("models", "tools")

    async def start(self, ctx: PluginContext) -> None:
        ctx.defer(ctx.services.get(MODELS).register("query_demo", ScriptedQueryAdapter()))

        async def lookup_order(arguments: dict[str, JsonValue]) -> str:
            # A real plugin calls its business API with host-provided credentials and scope.
            if arguments["order_id"] != "DEMO-001":
                raise ToolExecutionError("Order not found")
            return "DEMO-001: shipped (offline fixture)"

        ctx.defer(
            ctx.services.get(TOOLS).register(
                Tool(
                    ToolDefinition(
                        name="lookup_order",
                        description="Look up the status of a demo order.",
                        parameters={
                            "type": "object",
                            "properties": {"order_id": {"type": "string"}},
                            "required": ["order_id"],
                            "additionalProperties": False,
                        },
                    ),
                    lookup_order,
                )
            )
        )


async def main() -> None:
    async with Application(
        [
            ModelsPlugin(
                ModelsConfig(models={"qa": ModelSpec(adapter="query_demo", model="scripted")})
            ),
            ToolsPlugin(),
            QueryDemoPlugin(),
            MemoryPlugin(),
            AgentPlugin(AgentConfig(model="qa", system="Answer using query results.")),
            RunnerPlugin(RunnerConfig()),
        ]
    ) as app:
        handle = app.services.get(RUNNER).submit("Query order DEMO-001", session_id="demo-chat")
        result = await handle.result()
        if result.state != "succeeded" or result.message is None:
            raise RuntimeError(f"Query failed: {result.state}, {result.error}")
        print(result.message.content)


if __name__ == "__main__":
    asyncio.run(main())
