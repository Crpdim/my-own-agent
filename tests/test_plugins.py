import asyncio

import pytest

from my_own_agent.core.plugins import Application, BasePlugin
from my_own_agent.core.registry import ServiceKey

SERVICE = ServiceKey[str]("test")


class Recorder(BasePlugin):
    def __init__(self, name, log, requires=(), fail_start=False, fail_stop=False):
        self.name, self.log, self.requires = name, log, requires
        self.fail_start, self.fail_stop = fail_start, fail_stop

    async def start(self, ctx):
        self.log.append(f"start:{self.name}")
        ctx.defer(lambda: self.log.append(f"clean:{self.name}"))
        if self.name == "base":
            ctx.provide(SERVICE, "ready")
        if self.fail_start:
            raise ValueError("start failed")

    async def stop(self, ctx):
        self.log.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("stop failed")


async def test_dependency_order_and_reverse_cleanup():
    log: list[str] = []
    app = Application([Recorder("child", log, ("base",)), Recorder("base", log)])
    async with app:
        assert app.services.get(SERVICE) == "ready"
        assert log == ["start:base", "start:child"]
    assert log[2:] == ["stop:child", "clean:child", "stop:base", "clean:base"]
    with pytest.raises(LookupError):
        app.services.get(SERVICE)
    await app.close()  # Shutdown is idempotent.


@pytest.mark.parametrize("kind", ["cycle", "missing", "duplicate"])
async def test_invalid_dependencies_have_no_startup_side_effects(kind):
    log: list[str] = []
    plugins = {
        "cycle": [Recorder("a", log, ("b",)), Recorder("b", log, ("a",))],
        "missing": [Recorder("a", log, ("missing",))],
        "duplicate": [Recorder("a", log), Recorder("a", log)],
    }[kind]
    with pytest.raises(ValueError):
        await Application(plugins).start()
    assert log == []


async def test_partial_start_is_rolled_back():
    log: list[str] = []
    app = Application([Recorder("base", log), Recorder("bad", log, fail_start=True)])
    with pytest.raises(ValueError, match="start failed"):
        await app.start()
    assert log[-4:] == ["stop:bad", "clean:bad", "stop:base", "clean:base"]
    with pytest.raises(LookupError):
        app.services.get(SERVICE)


async def test_shutdown_failure_does_not_skip_other_cleanup():
    log: list[str] = []
    app = Application([Recorder("base", log), Recorder("bad", log, fail_stop=True)])
    await app.start()
    with pytest.raises(ExceptionGroup, match="shutdown"):
        await app.close()
    assert log[-3:] == ["clean:bad", "stop:base", "clean:base"]
    with pytest.raises(LookupError):
        app.services.get(SERVICE)


async def test_duplicate_service_rolls_back_without_replacing_owner():
    class Conflict(BasePlugin):
        name = "conflict"
        requires = ("base",)

        async def start(self, ctx):
            ctx.provide(SERVICE, "wrong")

    app = Application([Recorder("base", []), Conflict()])
    with pytest.raises(ValueError, match="already registered"):
        await app.start()
    with pytest.raises(LookupError):
        app.services.get(SERVICE)


async def test_subscriptions_are_removed_and_observer_failures_are_isolated():
    received = []

    async def listener(event):
        received.append(event)

    async def broken(event):
        raise ValueError("observer failed")

    class Observer(BasePlugin):
        name = "observer"

        async def start(self, ctx):
            ctx.subscribe("test", broken)
            ctx.subscribe("test", listener)

    app = Application([Observer()])
    async with app:
        await app.events.emit("test", "once")
    await app.events.emit("test", "twice")
    assert received == ["once"]


async def test_cancelled_start_releases_resources():
    entered = asyncio.Event()
    cleaned = []

    class Slow(BasePlugin):
        name = "slow"

        async def start(self, ctx):
            ctx.defer(lambda: cleaned.append(True))
            entered.set()
            await asyncio.Event().wait()

    app = Application([Slow()])
    task = asyncio.create_task(app.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]
