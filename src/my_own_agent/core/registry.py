"""Typed service keys with explicit, reversible registration."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class ServiceKey[T]:
    name: str


class Registry:
    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def provide[T](self, key: ServiceKey[T], service: T) -> Callable[[], None]:
        if key.name in self._services:
            raise ValueError(f"Service already registered: {key.name}")
        self._services[key.name] = service

        def remove() -> None:
            if self._services.get(key.name) is service:
                del self._services[key.name]

        return remove

    def get[T](self, key: ServiceKey[T]) -> T:
        if key.name not in self._services:
            raise LookupError(f"Service is not registered: {key.name}")
        return cast(T, self._services[key.name])
