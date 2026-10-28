"""Ordered observers; observer failures do not invalidate completed work."""

import logging
from collections.abc import Awaitable, Callable

type Listener = Callable[[object], Awaitable[None]]
logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def subscribe(self, topic: str, listener: Listener) -> Callable[[], None]:
        listeners = self._listeners.setdefault(topic, [])
        listeners.append(listener)

        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)

        return remove

    async def emit(self, topic: str, event: object) -> None:
        for listener in tuple(self._listeners.get(topic, ())):
            try:
                await listener(event)
            except Exception:
                logger.warning("Event observer failed for %s", topic)
