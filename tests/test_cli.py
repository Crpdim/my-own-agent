import asyncio
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from my_own_agent.agent.runner import AgentRunner, JobFinished
from my_own_agent.config import AppConfig, create_application
from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import (
    Message,
    ModelDone,
    ModelError,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)
from my_own_agent.plugins.cli import CLI
from my_own_agent.plugins.deepseek_billing import DeepSeekBillingMonitor
from tests.test_agent import call, make_agent, request_tools
from tests.test_deepseek_billing import balance
from tests.test_deepseek_billing import config as billing_config


def invoke(*args):
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "my_own_agent", *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def test_offline_cli_runs_the_tool_cycle():
    result = invoke("--config", "examples/demo.toml", "--prompt", "time")
    assert result.returncode == 0, result.stderr
    assert "Offline demo" in result.stdout
    assert "[tool] current_time" in result.stderr


def test_deepseek_config_checks_without_credentials():
    result = invoke("--config", "agent.example.toml", "--check")
    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout


def test_missing_credential_is_actionable_and_nonzero():
    result = invoke("--config", "agent.example.toml", "--prompt", "hello")
    assert result.returncode == 1
    assert "missing_credential" in result.stderr
    assert "DEEPSEEK_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_config_has_setup_instructions(tmp_path):
    result = invoke("--config", str(tmp_path / "missing.toml"))
    assert result.returncode == 2
    assert "agent.example.toml" in result.stderr


def test_invalid_config_does_not_echo_input_values(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('plugins = "secret-token-value"\n')
    result = invoke("--config", str(path), "--check")
    assert result.returncode == 2
    assert "Invalid configuration" in result.stderr
    assert "secret-token-value" not in result.stderr


async def test_external_plugin_factory_needs_no_builtin_registration(tmp_path, monkeypatch):
    module = tmp_path / "custom_plugin.py"
    module.write_text(
        "from my_own_agent.core.plugins import BasePlugin\n"
        "from my_own_agent.core.registry import ServiceKey\n"
        "class Custom(BasePlugin):\n"
        "    name = 'custom'\n"
        "    async def start(self, ctx):\n"
        "        ctx.provide(ServiceKey[str]('custom_value'), 'hello')\n"
        "def create_plugin(config):\n"
        "    return Custom()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = AppConfig.model_validate({"plugins": [{"factory": "custom_plugin:create_plugin"}]})
    async with create_application(config) as app:
        assert app.services.get(ServiceKey[str]("custom_value")) == "hello"


def test_example_configs_are_portable():
    for path in [Path("agent.example.toml"), Path("examples/demo.toml")]:
        assert "/Users/" not in path.read_text()


def test_piped_messages_are_all_processed_before_exit():
    result = subprocess.run(
        [sys.executable, "-m", "my_own_agent", "--config", "examples/demo.toml"],
        input="first\nsecond\n/exit\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Offline demo") == 2


@pytest.mark.parametrize(
    ("show_thinking", "ending"),
    [(False, "success"), (True, "success"), (False, "error"), (False, "cancel")],
)
async def test_cli_interactive_preserves_complete_lines(monkeypatch, capsys, show_thinking, ending):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent, adapter, _, _ = make_agent([])

    async def stream(model, request):
        if show_thinking:
            yield ThinkingDelta("thinking ")
        else:
            yield TextDelta("answer ")
        entered.set()
        await release.wait()
        if ending == "error":
            yield ModelError("unavailable", "Model unavailable")
            return
        if show_thinking:
            yield ThinkingDelta("done\ntrailing thought")
            yield TextDelta("answer ")
        yield TextDelta("done\ntrailing answer")
        yield ModelDone(Message(role="assistant", content="answer done\ntrailing answer"), "stop")

    async def observe(event):
        if isinstance(event, JobFinished):
            finished.set()

    monkeypatch.setattr(adapter, "stream", stream)
    # Capture the complete lines passed to the prompt-safe output layer directly.
    monkeypatch.setattr("my_own_agent.plugins.cli.patch_stdout", nullcontext)
    runner = AgentRunner(agent, agent.events)
    runner.start()
    unsubscribe = agent.events.subscribe("runner.event", observe)
    with create_pipe_input() as pipe, create_app_session(output=DummyOutput()):
        monkeypatch.setattr("my_own_agent.plugins.cli.create_input", lambda: pipe)
        task = asyncio.create_task(CLI(runner, agent.events).run(None, "a", show_thinking))
        try:
            async with asyncio.timeout(3):
                pipe.send_text("test\n")
                await entered.wait()
                partial = capsys.readouterr()
                assert "answer " not in partial.out
                assert "thinking " not in partial.err
                # A late report from another job must not split a response line.
                print("[balance previous] ready", file=sys.stderr)
                if ending == "cancel":
                    pipe.send_text("\x03")
                    assert await task == 130
                else:
                    release.set()
                    await finished.wait()
                    pipe.send_text("/exit\n")
                    assert await task == (1 if ending == "error" else 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.close(drain=False)
            unsubscribe()

    output = capsys.readouterr()
    assert "[balance previous] ready\n" in output.err
    if ending == "success":
        assert output.out == "answer done\ntrailing answer\n"
        if show_thinking:
            assert "thinking done\ntrailing thought\n" in output.err
    else:
        assert output.out == "answer \n"
        if ending == "error":
            assert "[unavailable] Model unavailable" in output.err


async def test_cli_single_prompt_keeps_incremental_output(monkeypatch, capsys):
    entered, release = asyncio.Event(), asyncio.Event()
    agent, adapter, _, _ = make_agent([])

    async def stream(model, request):
        yield TextDelta("answer ")
        entered.set()
        await release.wait()
        yield TextDelta("done")
        yield ModelDone(Message(role="assistant", content="answer done"), "stop")

    monkeypatch.setattr(adapter, "stream", stream)
    runner = AgentRunner(agent, agent.events)
    runner.start()
    task = asyncio.create_task(CLI(runner, agent.events).run("test", "a"))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            assert capsys.readouterr().out == "answer "
            release.set()
            assert await task == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.close(drain=False)
    assert capsys.readouterr().out == "done\n"


async def test_cli_billing_and_response_use_prompt_safe_output(monkeypatch, capsys):
    displayed = asyncio.Event()
    loop = asyncio.get_running_loop()
    writes: list[str] = []

    class RecordingOutput(DummyOutput):
        def write(self, data: str) -> None:
            writes.append(data)
            if "[balance " in data:
                loop.call_soon_threadsafe(displayed.set)

    agent, _, _, _ = make_agent(
        [
            [TextDelta("before tool"), request_tools(call())],
            [
                TextDelta("first "),
                TextDelta("line\nlast line"),
                UsageEvent(Usage(input_tokens=1)),
                ModelDone(Message(role="assistant", content="first line\nlast line"), "stop"),
            ],
        ]
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    # Match plugin startup: billing is constructed before the CLI patches stderr.
    monitor = DeepSeekBillingMonitor(billing_config(), fetch_balance=lambda _: balance())
    monitor.start()
    unsubscribe = agent.events.subscribe("runner.event", monitor.observe)
    runner = AgentRunner(agent, agent.events)
    runner.start()
    with create_pipe_input() as pipe, create_app_session(output=RecordingOutput()):
        monkeypatch.setattr("my_own_agent.plugins.cli.create_input", lambda: pipe)
        task = asyncio.create_task(CLI(runner, agent.events).run(None, "a"))
        try:
            async with asyncio.timeout(3):
                pipe.send_text("test\n")
                await displayed.wait()
                pipe.send_text("/exit\n")
                assert await task == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.close(drain=False)
            await monitor.close()
            unsubscribe()

    rendered = "".join(writes)
    assert "before tool\n" in rendered
    assert rendered.index("before tool\n") < rendered.index("[tool] double")
    assert "first line\nlast line\n" in rendered
    assert rendered.index("last line\n") < rendered.index("[billing ")
    assert "[balance " in rendered
    assert "[billing" not in capsys.readouterr().err


@pytest.mark.parametrize("exit_text", ["/exit\n", "\x04"])
async def test_cli_accepts_next_message_during_execution_and_drains_on_exit(monkeypatch, exit_text):
    entered, queued, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(args):
        entered.set()
        await release.wait()
        return "4"

    answer = ModelDone(Message(role="assistant", content="done"), "stop")
    agent, adapter, store, _ = make_agent(
        [[request_tools(call())], [answer], [answer]], handler=handler
    )
    runner = AgentRunner(agent, agent.events)
    runner.start()
    handles = []
    original_submit = runner.submit

    def submit(prompt, session_id):
        handle = original_submit(prompt, session_id)
        handles.append(handle)
        if len(handles) == 2:
            queued.set()
        return handle

    monkeypatch.setattr(runner, "submit", submit)
    with create_pipe_input() as pipe, create_app_session(output=DummyOutput()):
        monkeypatch.setattr("my_own_agent.plugins.cli.create_input", lambda: pipe)
        task = asyncio.create_task(CLI(runner, agent.events).run(None, "a"))
        try:
            async with asyncio.timeout(3):
                pipe.send_text("first\n")
                await entered.wait()
                pipe.send_text("second\n")
                await queued.wait()
                assert handles[0].state == "running" and handles[1].state == "queued"
                assert [m.content for m in await store.load("a") if m.role == "user"] == ["first"]
                pipe.send_text(exit_text)
                release.set()
                assert await task == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.close(drain=False)
    assert [h.state for h in handles] == ["succeeded", "succeeded"]
    assert len(adapter.requests) == 3


async def test_cli_ctrl_c_cancels_active_and_queued_jobs(monkeypatch):
    entered, queued = asyncio.Event(), asyncio.Event()

    async def handler(args):
        entered.set()
        await asyncio.Event().wait()

    agent, _, store, _ = make_agent([[request_tools(call())]], handler=handler)
    runner = AgentRunner(agent, agent.events)
    runner.start()
    handles = []
    original_submit = runner.submit

    def submit(prompt, session_id):
        handle = original_submit(prompt, session_id)
        handles.append(handle)
        if len(handles) == 2:
            queued.set()
        return handle

    monkeypatch.setattr(runner, "submit", submit)
    with create_pipe_input() as pipe, create_app_session(output=DummyOutput()):
        monkeypatch.setattr("my_own_agent.plugins.cli.create_input", lambda: pipe)
        task = asyncio.create_task(CLI(runner, agent.events).run(None, "a"))
        try:
            async with asyncio.timeout(3):
                pipe.send_text("first\n")
                await entered.wait()
                pipe.send_text("second\n")
                await queued.wait()
                pipe.send_text("\x03")
                assert await task == 130
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.close(drain=False)
    assert [h.state for h in handles] == ["cancelled", "cancelled"]
    history = await store.load("a")
    assert history[-1].tool_call_id == "one" and history[-1].is_error
    assert [m.content for m in history if m.role == "user"] == ["first"]
