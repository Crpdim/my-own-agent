from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.service import MODELS, ModelService
from my_own_agent.llm.types import DataModel, ModelSpec


class Config(DataModel):
    models: dict[str, ModelSpec]


class ModelsPlugin(BasePlugin):
    name = "models"

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(MODELS, ModelService(self.config.models))


def create_plugin(config: dict[str, JsonValue]) -> ModelsPlugin:
    return ModelsPlugin(Config.model_validate(config))
