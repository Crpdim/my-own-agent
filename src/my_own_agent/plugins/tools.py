from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel
from my_own_agent.tools.service import TOOLS, ToolService


class ToolsPlugin(BasePlugin):
    name = "tools"

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(TOOLS, ToolService())


def create_plugin(config: dict[str, JsonValue]) -> ToolsPlugin:
    DataModel.model_validate(config)
    return ToolsPlugin()
