"""Opt-in prompt composition. Replace this plugin to provide another PromptService."""

from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.prompts.composer import Config, TextPromptComposer
from my_own_agent.prompts.service import PROMPTS, PromptService


class PromptsPlugin(BasePlugin):
    name = "prompts"

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(PROMPTS, PromptService(TextPromptComposer(self.config)))


def create_plugin(config: dict[str, JsonValue]) -> PromptsPlugin:
    return PromptsPlugin(Config.model_validate(config))
