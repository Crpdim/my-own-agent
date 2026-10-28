from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel, ToolDefinition
from my_own_agent.tools.service import TOOLS, Tool


async def current_time(arguments: dict[str, JsonValue]) -> str:
    return datetime.now(ZoneInfo(str(arguments.get("timezone", "Asia/Shanghai")))).isoformat()


class ClockPlugin(BasePlugin):
    name = "clock"
    requires = ("tools",)

    async def start(self, ctx: PluginContext) -> None:
        ctx.defer(
            ctx.services.get(TOOLS).register(
                Tool(
                    ToolDefinition(
                        name="current_time",
                        description="Get the current time in an IANA timezone.",
                        parameters={
                            "type": "object",
                            "properties": {"timezone": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    ),
                    current_time,
                )
            )
        )


def create_plugin(config: dict[str, JsonValue]) -> ClockPlugin:
    DataModel.model_validate(config)
    return ClockPlugin()
