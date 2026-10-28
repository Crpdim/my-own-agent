from dataclasses import dataclass
from typing import assert_type

import pytest

from my_own_agent.agent.loop import AGENT, Agent
from my_own_agent.agent.runner import (
    RUNNER,
    AgentJob,
    AgentRunner,
    JobReceipt,
    JobResult,
    JobState,
    Runner,
)
from my_own_agent.core.plugins import Application, BasePlugin, PluginContext
from my_own_agent.llm.types import Message, ModelDone, ModelError, TextDelta
from my_own_agent.plugins.cli import CLI, INTERACTION, CLIPlugin
from tests.test_agent import make_agent


@dataclass(frozen=True)
class ReadOnlyReceipt:
    """An external receipt exposes no JobHandle implementation or writable fields."""

    source: JobReceipt

    @property
    def job(self) -> AgentJob:
        return self.source.job

    @property
    def state(self) -> JobState:
        return self.source.state

    async def result(self) -> JobResult:
        return await self.source.result()


class CustomRunner:
    """An independent service class; this test reuses FIFO execution by composition."""

    def __init__(self, backend: Runner) -> None:
        self.backend = backend
        self.receipts: list[ReadOnlyReceipt] = []

    def submit(self, prompt: str, session_id: str = "default") -> ReadOnlyReceipt:
        receipt = ReadOnlyReceipt(self.backend.submit(prompt, session_id))
        self.receipts.append(receipt)
        return receipt

    async def close(self, *, drain: bool = True) -> None:
        await self.backend.close(drain=drain)


class ExistingAgentPlugin(BasePlugin):
    name = "agent"

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(AGENT, self.agent)


class CustomRunnerPlugin(BasePlugin):
    name = "runner"
    requires = ("agent",)

    async def start(self, ctx: PluginContext) -> None:
        backend = AgentRunner(ctx.services.get(AGENT), ctx.events)
        backend.start()
        self.runner = CustomRunner(backend)
        # Explicit protocol assignments make this a static compatibility regression too.
        service: Runner = self.runner
        ctx.defer_async(service.close)
        ctx.provide(RUNNER, service)


@pytest.mark.parametrize("failed", [False, True])
async def test_cli_uses_custom_runner_plugin_and_read_only_receipt(capsys, failed):
    response = (
        [ModelError("unavailable", "Model unavailable")]
        if failed
        else [
            TextDelta("custom answer"),
            ModelDone(Message(role="assistant", content="custom answer"), "stop"),
        ]
    )
    agent, _, store, _ = make_agent([response])
    plugin = CustomRunnerPlugin()
    app = Application([CLIPlugin(), plugin, ExistingAgentPlugin(agent)])

    async with app:
        runner = assert_type(app.services.get(RUNNER), Runner)
        assert runner is plugin.runner
        # Direct construction must accept the external class as well as service lookup.
        CLI(plugin.runner, app.events)
        status = await app.services.get(INTERACTION).run("question", "external-chat")
        receipt: JobReceipt = plugin.runner.receipts[0]
        result = await receipt.result()
        assert await receipt.result() == result
        assert receipt.job.prompt == "question"
        assert receipt.job.session_id == "external-chat"
        assert receipt.state == result.state == ("failed" if failed else "succeeded")

    assert status == (1 if failed else 0)
    output = capsys.readouterr()
    if failed:
        assert "[unavailable] Model unavailable" in output.err
    else:
        assert output.out == "custom answer\n"
    assert (await store.load("external-chat"))[0].content == "question"
    with pytest.raises(LookupError):
        app.services.get(RUNNER)
    with pytest.raises(RuntimeError, match="not accepting"):
        runner.submit("late")


async def test_default_runner_satisfies_the_same_public_contract():
    agent, _, _, _ = make_agent([[ModelDone(Message(role="assistant", content="ok"), "stop")]])
    implementation = AgentRunner(agent, agent.events)
    service: Runner = implementation
    implementation.start()
    try:
        receipt = assert_type(service.submit("question"), JobReceipt)
        result = await receipt.result()
        assert receipt.state == "succeeded"
        assert result.message is not None and result.message.content == "ok"
    finally:
        await service.close(drain=False)
