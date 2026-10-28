import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakValueDictionary

from my_own_agent.core.clock import Clock, get_timezone, read_clock, utc_now
from my_own_agent.core.events import EventBus
from my_own_agent.core.registry import ServiceKey
from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.llm.service import ModelService
from my_own_agent.llm.types import (
    Message,
    ModelDone,
    ModelError,
    ModelEvent,
    ModelRequest,
    RuntimeState,
    ToolCall,
)
from my_own_agent.prompts.service import (
    PreparedPrompt,
    PromptBuildFailed,
    PromptBuilt,
    PromptService,
)
from my_own_agent.storage.service import SessionStore
from my_own_agent.tools.service import ToolService


@dataclass(frozen=True)
class ToolStarted:
    call: ToolCall


@dataclass(frozen=True)
class ToolFinished:
    call: ToolCall
    result: Message


@dataclass(frozen=True)
class TurnDone:
    message: Message


type AgentEvent = ModelEvent | ToolStarted | ToolFinished | TurnDone | PromptBuilt


@dataclass(frozen=True)
class AgentNotification:
    session_id: str
    event: AgentEvent


class Agent:
    """Execute turns with per-session exclusion within this instance and event loop."""

    def __init__(
        self,
        models: ModelService,
        tools: ToolService,
        sessions: SessionStore,
        events: EventBus,
        model: str,
        system: str,
        max_steps: int = 10,
        *,
        prompts: PromptService | None = None,
        timezone: str = "UTC",
        clock: Clock = utc_now,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.models, self.tools, self.sessions = models, tools, sessions
        self.events, self.model, self.system = events, model, system
        self.max_steps = max_steps
        self.prompts = prompts
        self.timezone = get_timezone(timezone)
        self.clock = clock
        self.capture_prompts = False
        # Active holders and waiters retain their lock; idle sessions need no cache entry.
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        # No await between lookup and insertion: callers on this event loop share one lock.
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def run(
        self,
        prompt: str,
        session_id: str = "default",
        *,
        submitted_at: datetime | None = None,
    ) -> AsyncGenerator[AgentEvent]:
        """Consume a turn; timestamp submission before waiting for this session's lock."""
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        user = Message(
            role="user",
            content=prompt,
            submitted_at=submitted_at if submitted_at is not None else read_clock(self.clock),
        )
        async with self._session_lock(session_id):
            async with aclosing(self._turn(user, session_id)) as stream:
                async for event in stream:
                    await self.events.emit("agent.event", AgentNotification(session_id, event))
                    yield event

    async def _turn(self, user: Message, session_id: str) -> AsyncGenerator[AgentEvent]:
        self.models.resolve(self.model)  # Fail before recording input for an invalid route.
        messages = await self.sessions.load(session_id)
        messages.append(user)
        await self.sessions.save(session_id, messages)
        for _ in range(self.max_steps):
            step_messages = self._prepare_step(messages)
            request = self._build_request(step_messages, submitted_at=user.submitted_at)
            if self.prompts is not None:
                try:
                    prepared = await self.prompts.prepare(request, capture=self.capture_prompts)
                except PromptBuildFailed as error:
                    yield PromptBuilt(error.inspection_json)
                    yield ModelError("prompt_build_failed", str(error))
                    return
                request = prepared.request
                yield PromptBuilt(prepared.inspection_json)
            response: ModelDone | None = None
            async with aclosing(self._request_model(request)) as stream:
                async for event in stream:
                    if isinstance(event, ModelDone):
                        response = event
                    else:
                        yield event
            if response is None:
                # The request stage has already emitted its terminal error.
                return
            message = response.message
            if self._should_stop(message):
                messages.append(message)
                await self.sessions.save(session_id, messages)
                # 一轮任务结束
                yield response
                yield TurnDone(message)
                return
            async with aclosing(self._execute_tools(response, messages, session_id)) as stream:
                async for tool_event in stream:
                    yield tool_event
        yield ModelError("step_limit", f"Stopped after {self.max_steps} model calls")

    async def inspect_prompt(
        self, prompt: str | None = None, session_id: str = "default"
    ) -> PreparedPrompt:
        """Capture an assembled preview without saving input or calling models/tools."""
        if self.prompts is None:
            raise ValueError("Enable the prompts plugin and agent.use_prompts to inspect prompts")
        async with self._session_lock(session_id):
            messages = list(await self.sessions.load(session_id))
            if prompt is not None:
                messages.append(Message(role="user", content=prompt))
            return await self.prompts.prepare(self._build_request(tuple(messages)), capture=True)

    def _prepare_step(self, messages: list[Message]) -> tuple[Message, ...]:
        """Project history for this step; future pre-step hooks can prepare context here."""
        return tuple(messages)

    def _build_request(
        self, messages: tuple[Message, ...], *, submitted_at: datetime | None = None
    ) -> ModelRequest:
        """Assemble the request; future request hooks can adjust call settings here."""
        runtime = None
        if self.prompts is not None:
            runtime = RuntimeState(
                current_time=read_clock(self.clock).astimezone(self.timezone),
                timezone=str(self.timezone),
                user_submitted_at=(
                    submitted_at.astimezone(self.timezone) if submitted_at is not None else None
                ),
            )
        return ModelRequest(
            model_alias=self.model,
            system=self.system,
            messages=messages,
            tools=self.tools.definitions(),
            runtime=runtime,
        )

    async def _request_model(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        """Stream one call, ending with a validated ModelDone or a ModelError."""
        response: ModelDone | None = None
        async with aclosing(self.models.stream(request)) as stream:
            async for event in stream:
                if isinstance(event, ModelError):
                    # Future request-error hooks belong here; currently errors end the turn.
                    yield event
                    return
                if isinstance(event, ModelDone):
                    response = event
                    break  # ModelDone is terminal by contract.
                yield event
        if response is None:
            yield ModelError("incomplete_stream", "Adapter ended without a terminal event")
            return
        if response.reason in {"length", "content_filter"}:
            yield ModelError(
                response.reason, "Model response was truncated or filtered; no tools executed"
            )
            return
        message = response.message
        if message.role != "assistant" or bool(message.tool_calls) != (
            response.reason == "tool_calls"
        ):
            yield ModelError("invalid_response", "Adapter returned an invalid assistant response")
            return
        if len({call.id for call in message.tool_calls}) != len(message.tool_calls):
            yield ModelError("invalid_response", "Adapter returned duplicate tool call IDs")
            return
        yield response

    async def _execute_tools(
        self, response: ModelDone, messages: list[Message], session_id: str
    ) -> AsyncGenerator[ModelDone | ToolStarted | ToolFinished]:
        """Execute a batch in order and record a result for every accepted tool call."""
        calls = response.message.tool_calls
        completed = 0
        saved_results = 0
        try:
            # Include the first save: it may commit before cancellation is delivered.
            messages.append(response.message)
            await self.sessions.save(session_id, messages)
            # Guard this yield too: a consumer may close before the first ToolStarted.
            yield response
            for call in calls:
                yield ToolStarted(call)
                # Future pre-execute hooks can authorize the call before execution.
                result = await self.tools.execute(call)
                # Future post-execute hooks can process the result before it is recorded.
                messages.append(result)
                completed += 1
                await self.sessions.save(session_id, messages)
                saved_results = completed
                yield ToolFinished(call, result)
        finally:
            # Early stream close / cancellation must not leave unmatched calls in history.
            # Tools may already have side effects; never silently replay them.
            if completed < len(calls) or saved_results < completed:
                for call in calls[completed:]:
                    messages.append(
                        self.tools.error(
                            call,
                            "Turn interrupted; tool completion is unconfirmed. "
                            "Verify effects before retrying.",
                        )
                    )
                # A result can exist locally even if its save was interrupted. Keep it
                # and wait for the repair save, including through repeated cancellation.
                await finish_before_cancel(
                    asyncio.create_task(self.sessions.save(session_id, messages))
                )

    def _should_stop(self, message: Message) -> bool:
        """Stop when no tools are requested; future turn-stopping hooks belong here."""
        return not message.tool_calls


AGENT = ServiceKey[Agent]("agent")
