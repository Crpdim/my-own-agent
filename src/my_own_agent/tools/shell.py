"""Bounded bash output and owned POSIX process-group cleanup."""

import asyncio
import math
import os
import signal
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.tools._local import MAX_OUTPUT_BYTES, MAX_OUTPUT_LINES
from my_own_agent.tools.service import ToolExecutionError


class _Capture(asyncio.SubprocessProtocol):
    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[int] = loop.create_future()
        self.closed: asyncio.Future[None] = loop.create_future()
        self.transport: asyncio.SubprocessTransport | None = None
        self.tail = bytearray()
        self.total_bytes = 0
        self.pipe_error = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.SubprocessTransport, transport)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        self.total_bytes += len(data)
        self.tail.extend(data)
        if len(self.tail) > MAX_OUTPUT_BYTES:
            del self.tail[:-MAX_OUTPUT_BYTES]

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        self.pipe_error |= exc is not None

    def process_exited(self) -> None:
        assert self.transport is not None
        code = self.transport.get_returncode()
        assert code is not None
        self.exited.set_result(code)

    def connection_lost(self, exc: Exception | None) -> None:
        self.pipe_error |= exc is not None
        self.closed.set_result(None)

    def output(self) -> str:
        # Invalid bytes become replacement characters; trim again to keep UTF-8 output bounded.
        decoded = bytes(self.tail).decode("utf-8", errors="replace")
        encoded = decoded.encode("utf-8")
        text = encoded[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        truncated = (
            self.total_bytes > len(self.tail)
            or len(encoded) > MAX_OUTPUT_BYTES
            or len(lines) > MAX_OUTPUT_LINES
        )
        text = "".join(lines[-MAX_OUTPUT_LINES:])
        if truncated:
            text = (
                "[Output truncated to its tail; redirect to a file to retain full output.]\n" + text
            )
        return text or "(no output)"


class BashTool:
    def __init__(self, cwd: Path, executable: str, timeout_seconds: float = 120) -> None:
        self.cwd, self.executable, self.timeout_seconds = cwd, executable, timeout_seconds

    async def execute(self, arguments: dict[str, JsonValue]) -> str:
        command = cast(str, arguments["command"])
        timeout = float(cast(float, arguments.get("timeout", self.timeout_seconds)))
        if not math.isfinite(timeout) or not 0 < timeout <= self.timeout_seconds:
            raise ToolExecutionError(
                f"timeout must be positive and at most {self.timeout_seconds:g}s."
            )
        capture = _Capture()
        # argv is passed directly: path/command are never interpolated into an extra shell command.
        spawn = asyncio.create_task(
            asyncio.get_running_loop().subprocess_exec(
                lambda: capture,
                self.executable,
                "--noprofile",
                "--norc",
                "-c",
                command,
                cwd=self.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        )
        try:
            async with asyncio.timeout(timeout):
                cancellation: asyncio.CancelledError | None = None
                try:
                    await asyncio.shield(spawn)
                    # This signal is independent of EOF, which descendants may hold open.
                    code = await asyncio.shield(capture.exited)
                except asyncio.CancelledError as error:
                    cancellation = error
                    raise
                finally:
                    # Includes cancellation during process creation and repeated cancellation.
                    try:
                        await finish_before_cancel(
                            asyncio.create_task(self._cleanup(spawn, capture))
                        )
                    except Exception as error:
                        if cancellation is not None:
                            raise cancellation from error
                        raise
        except TimeoutError as error:
            raise ToolExecutionError(
                f"{capture.output()}\n[Command timed out after {timeout:g}s; "
                "verify effects before retrying.]"
            ) from error
        except OSError as error:
            raise ToolExecutionError(f"Could not execute bash (errno={error.errno}).") from error
        output = capture.output()
        if capture.pipe_error:
            raise ToolExecutionError(f"{output}\n[Command output was interrupted during cleanup.]")
        if code != 0:
            raise ToolExecutionError(f"{output}\n[Command exited with code {code}.]")
        return f"{output}\n[Exit code: 0]"

    async def _cleanup(
        self,
        spawn: asyncio.Task[tuple[asyncio.SubprocessTransport, _Capture]],
        capture: _Capture,
    ) -> None:
        try:
            transport, _ = await spawn
        except Exception:
            return  # A failed spawn owns no successfully returned transport.
        try:
            try:
                os.killpg(transport.get_pid(), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(1):
                    await asyncio.shield(capture.closed)
            except TimeoutError:
                # A detached process can escape our group and retain a pipe. Do not hang
                # the agent on it; this is resource cleanup, not an OS sandbox.
                capture.pipe_error = True
        finally:
            transport.close()
            await capture.closed
