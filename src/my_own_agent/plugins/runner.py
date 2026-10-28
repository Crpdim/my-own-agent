"""Own the queue worker and stop it before agent dependencies are released."""

from pydantic import Field, JsonValue

from my_own_agent.agent.loop import AGENT
from my_own_agent.agent.runner import RUNNER, AgentRunner
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel


class Config(DataModel):
    max_pending: int = Field(default=32, ge=1)


class RunnerPlugin(BasePlugin):
    name = "runner"
    requires = ("agent",)

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        runner = AgentRunner(ctx.services.get(AGENT), ctx.events, self.config.max_pending)
        runner.start()
        ctx.defer_async(runner.close)
        ctx.provide(RUNNER, runner)


def create_plugin(config: dict[str, JsonValue]) -> RunnerPlugin:
    return RunnerPlugin(Config.model_validate(config))
