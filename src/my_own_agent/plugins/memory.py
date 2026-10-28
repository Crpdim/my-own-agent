from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel, Message
from my_own_agent.storage.service import SESSIONS


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, list[Message]] = {}

    async def load(self, session_id: str) -> list[Message]:
        return [message.model_copy(deep=True) for message in self._sessions.get(session_id, [])]

    async def save(self, session_id: str, messages: list[Message]) -> None:
        self._sessions[session_id] = [message.model_copy(deep=True) for message in messages]


class MemoryPlugin(BasePlugin):
    name = "storage"

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(SESSIONS, MemoryStore())


def create_plugin(config: dict[str, JsonValue]) -> MemoryPlugin:
    DataModel.model_validate(config)
    return MemoryPlugin()
