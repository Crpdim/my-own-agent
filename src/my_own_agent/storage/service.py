from typing import Protocol

from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import Message


class SessionStore(Protocol):
    async def load(self, session_id: str) -> list[Message]: ...
    async def save(self, session_id: str, messages: list[Message]) -> None: ...


SESSIONS = ServiceKey[SessionStore]("sessions")
