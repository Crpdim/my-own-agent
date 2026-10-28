"""CLI is a replaceable interaction service, not part of the agent loop."""

import asyncio
import sys
from contextlib import closing
from typing import Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_input
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic import JsonValue

from my_own_agent.agent.loop import ToolFinished, ToolStarted
from my_own_agent.agent.runner import RUNNER, JobEvent, JobFinished, JobStarted, Runner
from my_own_agent.core.events import EventBus
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import DataModel, TextDelta, ThinkingDelta


class Interaction(Protocol):
    async def run(
        self, prompt: str | None, session_id: str, show_thinking: bool = False
    ) -> int: ...


INTERACTION = ServiceKey[Interaction]("interaction")


class _ResponseOutput:
    """Keep partial interactive lines away from the prompt and background logs."""

    def __init__(self, *, line_buffered: bool) -> None:
        self._line_buffered = line_buffered
        self._pending = ""
        self._thinking = False

    def write(self, text: str, *, thinking: bool = False) -> None:
        if not self._line_buffered:
            print(text, end="", flush=True, file=sys.stderr if thinking else sys.stdout)
            return
        if thinking != self._thinking:
            self.finish_line()
            self._thinking = thinking
        self._pending += text
        complete, newline, self._pending = self._pending.rpartition("\n")
        if newline:
            print(complete, file=sys.stderr if thinking else sys.stdout)

    def finish_line(self) -> None:
        if self._pending:
            print(self._pending, file=sys.stderr if self._thinking else sys.stdout)
            self._pending = ""


class CLI:
    def __init__(self, runner: Runner, events: EventBus) -> None:
        self.runner = runner
        self.events = events

    async def run(self, prompt: str | None, session_id: str, show_thinking: bool = False) -> int:
        jobs: set[str] = set()
        failed = False
        interactive = prompt is None
        output = _ResponseOutput(line_buffered=interactive)

        async def display(notification: object) -> None:
            nonlocal failed
            if not isinstance(notification, JobStarted | JobEvent | JobFinished):
                return
            if notification.job.job_id not in jobs:
                return
            label = notification.job.job_id[:8]
            if isinstance(notification, JobStarted):
                if interactive:
                    print(f"\n[job {label}] running", file=sys.stderr)
            elif isinstance(notification, JobEvent):
                match notification.event:
                    case TextDelta(text):
                        output.write(text)
                    case ThinkingDelta(text) if show_thinking:
                        output.write(text, thinking=True)
                    case ToolStarted(call):
                        output.finish_line()
                        print(f"\n[tool] {call.name}", file=sys.stderr)
                    case ToolFinished(call, result) if result.is_error:
                        print(f"[tool error] {call.name}: {result.content}", file=sys.stderr)
            else:
                output.finish_line()
                if not interactive:
                    print()
                if notification.result.error is not None:
                    error = notification.result.error
                    print(f"[{error.code}] {error.message}", file=sys.stderr)
                failed |= notification.result.state != "succeeded"
                if interactive:
                    print(f"[job {label}] {notification.result.state}", file=sys.stderr)
                jobs.remove(notification.job.job_id)

        unsubscribe = self.events.subscribe("runner.event", display)
        try:
            if prompt is not None:
                handle = self.runner.submit(prompt, session_id)
                jobs.add(handle.job.job_id)
                result = await handle.result()
                await self.runner.close()
                return 0 if result.state == "succeeded" else 1
            print(
                "Agent — messages queue while working. "
                "/exit or EOF drains the queue; Ctrl-C cancels. History is kept in memory."
            )
            # Async terminal input lets the worker run; patch_stdout preserves the input line.
            with closing(create_input()) as terminal_input, patch_stdout():
                session = PromptSession[str](input=terminal_input)
                while True:
                    try:
                        text = await session.prompt_async("you> ")
                    except EOFError:
                        break
                    if text.strip() == "/exit":
                        break
                    if not text.strip():
                        continue
                    try:
                        handle = self.runner.submit(text, session_id)
                    except asyncio.QueueFull:
                        print("Queue is full; message was not accepted. Try again later.")
                        failed = True
                        continue
                    jobs.add(handle.job.job_id)
                    print(f"[job {handle.job.job_id[:8]}] queued", file=sys.stderr)
                await self.runner.close()
            return 1 if failed else 0
        except KeyboardInterrupt:
            await self.runner.close(drain=False)
            return 130
        except asyncio.CancelledError:
            await self.runner.close(drain=False)
            raise
        finally:
            output.finish_line()
            unsubscribe()


class CLIPlugin(BasePlugin):
    name = "cli"
    requires = ("runner",)

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(INTERACTION, CLI(ctx.services.get(RUNNER), ctx.events))


def create_plugin(config: dict[str, JsonValue]) -> CLIPlugin:
    DataModel.model_validate(config)
    return CLIPlugin()
