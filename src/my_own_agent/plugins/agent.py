from pydantic import Field, JsonValue, field_validator

from my_own_agent.agent.loop import AGENT, Agent
from my_own_agent.core.clock import get_timezone
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.service import MODELS
from my_own_agent.llm.types import DataModel
from my_own_agent.prompts.service import PROMPTS
from my_own_agent.storage.service import SESSIONS
from my_own_agent.tools.service import TOOLS


class Config(DataModel):
    model: str
    system: str = (
        "You are a work assistant. Help with projects, files and tasks using verified facts."
    )
    max_steps: int = Field(default=8, ge=1)
    use_prompts: bool = False
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_timezone(value)
        return value


class AgentPlugin(BasePlugin):
    name = "agent"
    requires: tuple[str, ...] = ("models", "tools", "storage")

    def __init__(self, config: Config) -> None:
        self.config = config
        if config.use_prompts:
            self.requires = (*self.requires, "prompts")

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(
            AGENT,
            Agent(
                ctx.services.get(MODELS),
                ctx.services.get(TOOLS),
                ctx.services.get(SESSIONS),
                ctx.events,
                self.config.model,
                self.config.system,
                self.config.max_steps,
                prompts=ctx.services.get(PROMPTS) if self.config.use_prompts else None,
                timezone=self.config.timezone,
            ),
        )


def create_plugin(config: dict[str, JsonValue]) -> AgentPlugin:
    return AgentPlugin(Config.model_validate(config))
