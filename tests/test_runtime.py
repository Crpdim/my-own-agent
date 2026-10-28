import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from jsonschema import validate
from pydantic import ValidationError

from my_own_agent.agent.runner import AgentRunner
from my_own_agent.contracts.v1.prompt import PromptInspection
from my_own_agent.llm.types import Message, ModelDone, ModelRequest, RuntimeState
from my_own_agent.plugins.agent import Config as AgentConfig
from my_own_agent.plugins.openai_chat import ChatOptions, serialize_messages
from my_own_agent.prompts.composer import Config, TextPromptComposer
from my_own_agent.prompts.service import PromptBuildFailed, PromptBuilt, PromptService
from tests.test_agent import call, make_agent, request_tools


@dataclass
class ManualClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def timed_agent(responses, clock, *, timezone="Asia/Shanghai", handler=None):
    agent, adapter, store, executions = make_agent(responses, handler=handler)
    agent.clock = clock
    agent.timezone = ZoneInfo(timezone)
    agent.prompts = PromptService(TextPromptComposer(Config()))
    return agent, adapter, store, executions


def answer():
    return ModelDone(Message(role="assistant", content="done"), "stop")


@pytest.mark.parametrize(
    "timezone,before,after,first_local,next_local",
    [
        (
            "Asia/Shanghai",
            "2026-09-11T15:59:50+00:00",
            "2026-09-11T16:01:00+00:00",
            "2026-09-11T23:59:50+08:00",
            "2026-09-12T00:01:00+08:00",
        ),
        (
            "America/New_York",
            "2026-11-01T05:59:00+00:00",
            "2026-11-01T06:01:00+00:00",
            "2026-11-01T01:59:00-04:00",
            "2026-11-01T01:01:00-05:00",
        ),
    ],
)
async def test_tool_round_refreshes_now_preserves_submission_and_traces_state(
    timezone, before, after, first_local, next_local
):
    clock = ManualClock(datetime.fromisoformat(before))
    executions = []

    async def advance(args):
        executions.append(args["value"])
        clock.value = datetime.fromisoformat(after)
        return "4"

    agent, adapter, store, _ = timed_agent(
        [[request_tools(call())], [answer()]], clock, timezone=timezone, handler=advance
    )
    agent.capture_prompts = True
    events = [event async for event in agent.run("整理今天的任务")]
    reports = [
        PromptInspection.model_validate_json(event.inspection_json)
        for event in events
        if isinstance(event, PromptBuilt)
    ]
    assert len(adapter.requests) == len(reports) == 2
    for request, report, now in zip(
        adapter.requests, reports, (first_local, next_local), strict=True
    ):
        expected = {
            "current_time": now,
            "timezone": timezone,
            "user_submitted_at": first_local,
        }
        assert request.runtime.model_dump(mode="json") == expected
        assert request.system.count("Runtime state (host-provided for this request):") == 1
        assert json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")) in (
            request.system
        )
        source = next(source for source in report.build.sources if source.kind == "runtime")
        contents = {view.ref.content_id: view.inline for view in report.contents}
        assert contents[source.content.content_id] == expected
        assert any(placement.target == "/runtime" for placement in report.build.placements)
        derived = next(
            fragment for fragment in report.build.fragments if fragment.key == "runtime.text"
        )
        assert any(
            operation.action == "transform" and derived.fragment_id in operation.output_fragment_ids
            for operation in report.build.operations
        )
        assert any(
            placement.target == "/system" and placement.fragment_id == derived.fragment_id
            for placement in report.build.placements
        )
        assert report.snapshot is not None
        assert contents[report.snapshot.request.content_id] == request.model_dump(mode="json")
        validate(report.model_dump(mode="json"), PromptInspection.model_json_schema())
        wire = serialize_messages(request, ChatOptions(base_url="https://example.invalid"))
        assert wire[0] == {"role": "system", "content": request.system}
        assert wire[1] == {"role": "user", "content": "整理今天的任务"}
    history = await store.load("default")
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[0].submitted_at == datetime.fromisoformat(before)
    assert all(message.submitted_at is None for message in history[1:])
    assert all("Runtime state" not in message.content for message in history)
    assert executions == [2]


async def test_queued_task_keeps_acceptance_time_across_midnight():
    clock = ManualClock(datetime.fromisoformat("2026-09-11T15:00:00+00:00"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(args):
        entered.set()
        await release.wait()
        return "4"

    agent, adapter, store, _ = timed_agent(
        [[request_tools(call())], [answer()], [answer()]], clock, handler=wait
    )
    runner = AgentRunner(agent, agent.events, clock=clock)
    runner.start()
    try:
        first = runner.submit("first")
        async with asyncio.timeout(3):
            await entered.wait()
            accepted = datetime.fromisoformat("2026-09-11T15:59:00+00:00")
            clock.value = accepted
            second = runner.submit("今天的任务")
            assert second.state == "queued"
            assert second.job.submitted_at == accepted
            clock.value = datetime.fromisoformat("2026-09-11T16:02:00+00:00")
            release.set()
            await runner.close()
        assert (await first.result()).state == (await second.result()).state == "succeeded"
        assert adapter.requests[2].runtime.model_dump(mode="json") == {
            "current_time": "2026-09-12T00:02:00+08:00",
            "timezone": "Asia/Shanghai",
            "user_submitted_at": "2026-09-11T23:59:00+08:00",
        }
        users = [message for message in await store.load("default") if message.role == "user"]
        assert [message.submitted_at for message in users] == [first.job.submitted_at, accepted]
    finally:
        release.set()
        await runner.close(drain=False)


@pytest.mark.parametrize("prompt", [None, "今天的任务"])
async def test_preview_refreshes_state_without_inventing_submission_or_saving(prompt):
    clock = ManualClock(datetime.fromisoformat("2026-09-11T12:00:00+00:00"))
    agent, adapter, store, _ = timed_agent([], clock)
    history = [Message(role="user", content="older task", submitted_at=clock.value)]
    await store.save("default", history)
    first = await agent.inspect_prompt(prompt)
    clock.value = datetime.fromisoformat("2026-09-12T12:00:00+00:00")
    second = await agent.inspect_prompt(prompt)
    assert first.request.runtime.current_time.isoformat() == "2026-09-11T20:00:00+08:00"
    assert second.request.runtime.current_time.isoformat() == "2026-09-12T20:00:00+08:00"
    assert (
        first.request.runtime.user_submitted_at is second.request.runtime.user_submitted_at is None
    )
    assert (
        len(first.request.messages) == len(second.request.messages) == (1 if prompt is None else 2)
    )
    assert await store.load("default") == history
    assert adapter.requests == []


async def test_disabled_prompts_keeps_original_request_and_wire_format():
    agent, adapter, store, _ = make_agent([[answer()]])
    agent.clock = ManualClock(datetime.fromisoformat("2026-09-11T12:00:00+00:00"))
    _ = [event async for event in agent.run("hello")]
    request = adapter.requests[0]
    assert request.system == "system" and request.runtime is None
    assert serialize_messages(request, ChatOptions(base_url="https://example.invalid")) == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


async def test_runtime_values_are_not_captured_by_default():
    clock = ManualClock(datetime.fromisoformat("2026-09-11T12:00:00+00:00"))
    agent, _, _, _ = timed_agent([[answer()]], clock)
    events = [event async for event in agent.run("hello")]
    record = next(event.inspection_json for event in events if isinstance(event, PromptBuilt))
    report = PromptInspection.model_validate_json(record)
    assert any(source.kind == "runtime" for source in report.build.sources)
    assert "2026-09-11" not in record and "Asia/Shanghai" not in record
    assert all(view.inline is None and view.sha256 is None for view in report.contents)


async def test_tracked_composer_must_attribute_runtime():
    class Untracked:
        name, version = "untracked", "1"

        async def compose(self, request, recorder):
            return request

    request = ModelRequest(
        model_alias="test",
        runtime=RuntimeState(
            current_time=datetime.fromisoformat("2026-09-11T12:00:00+00:00"), timezone="UTC"
        ),
    )
    with pytest.raises(PromptBuildFailed) as failure:
        await PromptService(Untracked()).prepare(request)
    report = PromptInspection.model_validate_json(failure.value.inspection_json)
    assert report.build.error is not None and "runtime" in report.build.error


@pytest.mark.parametrize("timezone", ["", "Unknown/Zone", "../UTC"])
def test_invalid_timezone_fails_configuration(timezone):
    with pytest.raises(ValidationError, match="IANA timezone"):
        AgentConfig(model="test", timezone=timezone)


def test_naive_state_time_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        RuntimeState(current_time=datetime(2026, 9, 11), timezone="UTC")


async def test_naive_clock_is_rejected_before_input_is_recorded_or_queued():
    clock = ManualClock(datetime(2026, 9, 11))
    agent, adapter, store, _ = timed_agent([], clock)
    with pytest.raises(ValueError, match="timezone-aware"):
        _ = [event async for event in agent.run("hello")]
    runner = AgentRunner(agent, agent.events, clock=clock)
    runner.start()
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            runner.submit("hello")
    finally:
        await runner.close()
    assert await store.load("default") == []
    assert adapter.requests == []
