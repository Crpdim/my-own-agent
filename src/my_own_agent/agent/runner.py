"""Runner contracts and a bounded, in-process FIFO implementation."""

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import uuid4

from my_own_agent.agent.loop import Agent, AgentEvent, TurnDone
from my_own_agent.core.clock import Clock, read_clock, utc_now
from my_own_agent.core.events import EventBus
from my_own_agent.core.registry import ServiceKey
from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.llm.types import Message, ModelError

logger = logging.getLogger(__name__)
type JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True)
class AgentJob:
    job_id: str
    session_id: str
    prompt: str
    submitted_at: datetime | None = None


@dataclass(frozen=True)
class JobResult:
    state: Literal["succeeded", "failed", "cancelled"]
    message: Message | None = None
    error: ModelError | None = None


class JobReceipt(Protocol):
    """Read-only job view; the owning runner controls execution and completion."""

    @property
    def job(self) -> AgentJob:
        """Stable identity and input for this accepted submission."""
        ...

    @property
    def state(self) -> JobState:
        """Current execution state; terminal states never change."""
        ...

    async def result(self) -> JobResult:
        """Wait for a stable result after cleanup, without cancelling the owned job.

        Cancelling a waiter cancels only that wait. Other and later waiters must
        still receive the job's terminal result.
        """
        ...


class Runner(Protocol):
    """Submission and shutdown contract, independent of scheduling policy.

    Plugins own implementation-specific startup. Accepted work runs without a
    result waiter and publishes JobStarted/JobEvent/JobFinished on runner.event
    after submit returns. Each job gets one terminal receipt and JobFinished;
    unstarted cancelled jobs do not emit JobStarted. See docs/plugins.md for the
    complete event, resource ownership and cancellation contract.
    """

    def submit(self, prompt: str, session_id: str = "default") -> JobReceipt:
        """Accept work immediately or reject it without recording input.

        Raise ValueError for blank input, asyncio.QueueFull for exhausted
        capacity, or RuntimeError after shutdown has begun.
        """
        ...

    async def close(self, *, drain: bool = True) -> None:
        """Stop accepting; drain accepted jobs or cancel them when drain=False.

        Wait for owned execution, stream cleanup and final notifications before
        returning. Repeated calls are safe. Cancelling this wait requests abort,
        waits for cleanup, then propagates cancellation.
        """
        ...


class JobHandle:
    """FIFO runner's receipt implementation; consumers use the JobReceipt contract."""

    def __init__(self, job: AgentJob) -> None:
        self.job = job
        self._state: JobState = "queued"
        self._result: asyncio.Future[JobResult] = asyncio.get_running_loop().create_future()

    @property
    def state(self) -> JobState:
        return self._state

    async def result(self) -> JobResult:
        """Wait for execution and stream cleanup; results remain available for later waiters."""
        return await asyncio.shield(self._result)

    def _finish(self, result: JobResult) -> None:
        self._state = result.state
        self._result.set_result(result)


@dataclass(frozen=True)
class JobStarted:
    job: AgentJob


@dataclass(frozen=True)
class JobEvent:
    job: AgentJob
    event: AgentEvent


@dataclass(frozen=True)
class JobFinished:
    job: AgentJob
    result: JobResult


class AgentRunner:
    """One worker executes all sessions in submission order; history stays session-scoped."""

    def __init__(
        self, agent: Agent, events: EventBus, max_pending: int = 32, *, clock: Clock = utc_now
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.agent = agent
        self.events = events
        self._clock = clock
        self._queue: asyncio.Queue[JobHandle] = asyncio.Queue(maxsize=max_pending)
        self._worker: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._accepting = False
        self._abort_requested = False

    def start(self) -> None:
        """Start the owned worker on the current event loop. A runner is single-use."""
        if self._worker is not None or self._closing is not None:
            raise RuntimeError("Runner can only be started once")
        self._worker = asyncio.create_task(self._work(), name="agent-runner")
        self._accepting = True

    def submit(self, prompt: str, session_id: str = "default") -> JobHandle:
        """Enqueue without waiting; raises QueueFull when pending capacity is exhausted."""
        if not self._accepting:
            raise RuntimeError("Runner is not accepting tasks")
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        handle = JobHandle(AgentJob(uuid4().hex, session_id, prompt, read_clock(self._clock)))
        # Only Agent.run writes messages, after this task reaches the head of the queue.
        self._queue.put_nowait(handle)
        return handle

    async def close(self, *, drain: bool = True) -> None:
        """Stop submissions and drain, or cancel all work. Cancellation forces cleanup."""
        self._accepting = False
        self._queue.shutdown()
        if not drain:
            self._request_abort()
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_shutdown(), name="agent-runner-close")
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            self._request_abort()
            # Repeated cancellation must not release dependencies before owned work settles.
            await finish_before_cancel(self._closing)
            raise

    def _request_abort(self) -> None:
        # Repeated close calls must not inject another cancellation into async cleanup.
        if not self._abort_requested:
            self._abort_requested = True
            if self._worker is not None and not self._worker.done():
                self._worker.cancel()

    async def _work(self) -> None:
        try:
            # Cleanup may replace CancelledError with a regular exception. The stop
            # decision must survive that conversion and prevent starting another job.
            while not self._abort_requested:
                try:
                    handle = await self._queue.get()
                except asyncio.QueueShutDown:
                    return
                try:
                    handle._state = "running"
                    await self.events.emit("runner.event", JobStarted(handle.job))
                    if self._abort_requested:
                        raise asyncio.CancelledError
                    result = await self._execute(handle.job)
                    if self._abort_requested and result.state == "succeeded":
                        result = JobResult("cancelled")
                    # _execute exits aclosing before resolving the receipt.
                    handle._finish(result)
                    await self.events.emit("runner.event", JobFinished(handle.job, result))
                except asyncio.CancelledError:
                    if not handle._result.done():
                        result = JobResult("cancelled")
                        handle._finish(result)
                        await self.events.emit("runner.event", JobFinished(handle.job, result))
                    raise
                finally:
                    self._queue.task_done()
        finally:
            self._accepting = False
            self._queue.shutdown()
            await self._cancel_pending()

    async def _execute(self, job: AgentJob) -> JobResult:
        message: Message | None = None
        error: ModelError | None = None
        try:
            async with aclosing(
                self.agent.run(job.prompt, job.session_id, submitted_at=job.submitted_at)
            ) as stream:
                async for event in stream:
                    if isinstance(event, TurnDone):
                        message = event.message
                    elif isinstance(event, ModelError):
                        error = event
                    await self.events.emit("runner.event", JobEvent(job, event))
        except Exception as exc:
            # Do not echo prompts, credentials or provider error bodies from raw exceptions.
            logger.error("Agent job %s failed (%s)", job.job_id, type(exc).__name__)
            return JobResult(
                "failed", error=ModelError("execution_error", "Agent execution failed")
            )
        if error is not None:
            return JobResult("failed", error=error)
        if message is None:
            return JobResult(
                "failed",
                error=ModelError("incomplete_turn", "Agent ended without a terminal event"),
            )
        return JobResult("succeeded", message=message)

    async def _cancel_pending(self) -> None:
        while True:
            try:
                handle = self._queue.get_nowait()
            except (asyncio.QueueEmpty, asyncio.QueueShutDown):
                return
            result = JobResult("cancelled")
            handle._finish(result)
            self._queue.task_done()
            await self.events.emit("runner.event", JobFinished(handle.job, result))

    async def _finish_shutdown(self) -> None:
        try:
            if self._worker is not None:
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass  # The runner owns and has settled cancellation of its worker.
        finally:
            # Also handles cancelling the worker before its coroutine ever started.
            await self._cancel_pending()


RUNNER = ServiceKey[Runner]("runner")
