"""Dependency-ordered, single-use application lifecycle."""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Protocol, Self

from .events import EventBus, Listener
from .registry import Registry, ServiceKey


class PluginContext:
    def __init__(self, services: Registry, events: EventBus) -> None:
        self.services = services
        self.events = events
        self._cleanup = AsyncExitStack()

    def provide[T](self, key: ServiceKey[T], service: T) -> None:
        self.defer(self.services.provide(key, service))

    def defer(self, cleanup: Callable[[], None]) -> None:
        self._cleanup.callback(cleanup)

    def defer_async(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        self._cleanup.push_async_callback(cleanup)

    def subscribe(self, topic: str, listener: Listener) -> None:
        self.defer(self.events.subscribe(topic, listener))

    async def close(self) -> None:
        await self._cleanup.aclose()


class Plugin(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def requires(self) -> tuple[str, ...]: ...

    async def start(self, ctx: PluginContext) -> None: ...
    async def stop(self, ctx: PluginContext) -> None: ...


class BasePlugin:
    """Override start; register resource cleanup on its context immediately."""

    name: str
    requires: tuple[str, ...] = ()

    async def start(self, ctx: PluginContext) -> None:
        pass

    async def stop(self, ctx: PluginContext) -> None:
        pass


class Application:
    def __init__(self, plugins: Sequence[Plugin]) -> None:
        self.services = Registry()
        self.events = EventBus()
        self._plugins = tuple(plugins)
        self._active: list[tuple[Plugin, PluginContext]] = []
        self._state = "new"

    def _ordered(self) -> list[Plugin]:
        by_name = {plugin.name: plugin for plugin in self._plugins}
        if len(by_name) != len(self._plugins):
            raise ValueError("Duplicate plugin names")
        ordered: list[Plugin] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Plugin dependency cycle at: {name}")
            if name in visited:
                return
            if name not in by_name:
                raise ValueError(f"Missing required plugin: {name}")
            visiting.add(name)
            for dependency in by_name[name].requires:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(by_name[name])

        for name in by_name:
            visit(name)
        return ordered

    async def start(self) -> None:
        if self._state != "new":
            raise RuntimeError("Application instances can only be started once")
        ordered = self._ordered()  # Validate before any plugin has side effects.
        self._state = "starting"
        try:
            for plugin in ordered:
                ctx = PluginContext(self.services, self.events)
                self._active.append((plugin, ctx))
                await plugin.start(ctx)
            self._state = "running"
        except BaseException as original:
            try:
                await self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup(
                    "Startup and cleanup failed", [original, cleanup]
                ) from None
            raise

    async def close(self) -> None:
        errors: list[BaseException] = []
        while self._active:
            plugin, ctx = self._active.pop()
            try:
                await plugin.stop(ctx)
            except BaseException as error:
                errors.append(error)
            try:
                await ctx.close()
            except BaseException as error:
                errors.append(error)
        self._state = "closed"
        if errors:
            raise BaseExceptionGroup("Plugin shutdown failed", errors)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.close()
        except BaseException as cleanup:
            if exc is not None:
                raise BaseExceptionGroup("Application and cleanup failed", [exc, cleanup]) from None
            raise
