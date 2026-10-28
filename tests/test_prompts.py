import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from jsonschema import ValidationError as SchemaError
from jsonschema import validate
from pydantic import TypeAdapter, ValidationError

from my_own_agent.contracts.v1.prompt import ContentView, PromptInspection
from my_own_agent.llm.types import Message, ModelDone, ModelError, ModelRequest, ToolDefinition
from my_own_agent.prompts.composer import Config, Section, TextPromptComposer
from my_own_agent.prompts.service import (
    PromptBuildCancelled,
    PromptBuildFailed,
    PromptBuilt,
    PromptService,
)
from tests.test_agent import call, make_agent, request_tools
from tests.test_cli import invoke


def service(**config):
    return PromptService(TextPromptComposer(Config(**config)))


def inspected(prepared):
    return PromptInspection.model_validate_json(prepared.inspection_json)


def content(report, ref):
    view = next(view for view in report.contents if view.ref.content_id == ref.content_id)
    assert view.ref.model_dump() == ref.model_dump()
    return view.inline


async def test_ordered_sources_preserve_unicode_whitespace_and_map_the_whole_request(tmp_path):
    path = tmp_path / "rules.md"
    path.write_bytes(b"\xef\xbb\xbf" + "文件\r\n ".encode())
    request = ModelRequest(
        model_alias="demo",
        system="基础😀",
        messages=(Message(role="user", content="问题"),),
        tools=(
            ToolDefinition(name="read", description="Read text", parameters={"type": "object"}),
        ),
    )
    prepared = await service(
        cwd=str(tmp_path),
        include_tool_descriptions=True,
        sections=(Section(key="inline", text=" first "), Section(key="file", path="rules.md")),
    ).prepare(request, capture=True)
    assert prepared.request.system == "基础😀\n\n first \n\n文件\r\n \n\nread: Read text"
    assert prepared.request.messages == request.messages and prepared.request.tools == request.tools
    assert request.system == "基础😀"
    report = inspected(prepared)
    assert report.snapshot is not None and report.snapshot.stage == "assembled"
    assert content(report, report.snapshot.request) == prepared.request.model_dump(mode="json")
    fragments = {fragment.fragment_id: fragment for fragment in report.build.fragments}
    for placement in report.build.placements:
        value = content(report, fragments[placement.fragment_id].content)
        if placement.target == "/system":
            assert prepared.request.system[placement.start : placement.end] == value
    assert {p.target for p in report.build.placements} == {"/system", "/tools/0", "/messages/0"}
    derived = next(op for op in report.build.operations if op.action == "transform")
    assert fragments[derived.input_fragment_ids[0]].key == "tool.read"
    assert fragments[derived.output_fragment_ids[0]].key == "tool_description.read"
    assert [op.order for op in report.build.operations] == list(
        range(1, len(report.build.operations) + 1)
    )
    file_source = next(source for source in report.build.sources if source.kind == "file")
    assert file_source.locator == str(path)
    for view in report.contents:
        raw = (
            view.inline
            if isinstance(view.inline, str)
            else json.dumps(
                view.inline,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ).encode()
        assert view.size_bytes == len(raw)
        assert view.sha256 == hashlib.sha256(raw).hexdigest()
    validate(json.loads(prepared.inspection_json), PromptInspection.model_json_schema())


async def test_empty_and_disabled_sections_add_no_content_or_file_access(tmp_path):
    prepared = await service(
        cwd=str(tmp_path),
        sections=(Section(key="off", path="missing", enabled=False),),
    ).prepare(ModelRequest(model_alias="demo"), capture=True)
    report = inspected(prepared)
    assert prepared.request.system == ""
    assert report.build.placements == ()
    assert [op.action for op in report.build.operations] == ["skip", "skip"]
    source = report.build.sources[-1]
    assert source.status == "skipped" and source.content.capture_state == "not_captured"
    assert report.build.operations[-1].source_ids == (source.source_id,)


async def test_metadata_capture_has_no_prompt_body_or_digest():
    prepared = await service().prepare(
        ModelRequest(
            model_alias="demo",
            system="private-system-value",
            messages=(Message(role="user", content="private-user-value"),),
        )
    )
    report = inspected(prepared)
    assert "private-system-value" not in prepared.inspection_json
    assert "private-user-value" not in prepared.inspection_json
    assert all(
        v.state == "not_captured" and v.inline is None and v.sha256 is None for v in report.contents
    )
    assert prepared.request.system == "private-system-value"


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8", "too_large", "fifo"])
async def test_unusable_file_fails_with_a_safe_source_record(tmp_path, kind):
    path = tmp_path / "source"
    if kind == "directory":
        path.mkdir()
    elif kind == "invalid_utf8":
        path.write_bytes(b"private-file-value\xff")
    elif kind == "too_large":
        path.write_text("private-file-value" * 10)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO requires POSIX")
        os.mkfifo(path)
    async with asyncio.timeout(3):
        with pytest.raises(PromptBuildFailed) as failure:
            await service(
                cwd=str(tmp_path),
                max_source_bytes=64,
                sections=(Section(key="file", path="source"),),
            ).prepare(
                ModelRequest(model_alias="demo"),
                capture=True,
            )
    report = PromptInspection.model_validate_json(failure.value.inspection_json)
    assert report.build.status == "failed" and report.snapshot is None
    assert report.build.sources[-1].status == "failed"
    assert "private-file-value" not in failure.value.inspection_json


@pytest.mark.parametrize(
    "config",
    [
        {"max_source_bytes": 2, "sections": (Section(key="large", text="你好"),)},
        {
            "max_system_bytes": 5,
            "sections": (Section(key="a", text="abc"), Section(key="b", text="d")),
        },
    ],
)
async def test_text_and_total_budgets_fail_instead_of_silent_truncation(config):
    with pytest.raises(PromptBuildFailed) as failure:
        await service(**config).prepare(ModelRequest(model_alias="demo"))
    assert inspected(failure.value).build.status == "failed"


@pytest.mark.parametrize(
    "config",
    [
        {"sections": [{"key": "a"}]},
        {"sections": [{"key": "a", "text": "", "path": "file"}]},
        {"sections": [{"key": "a", "text": ""}, {"key": "a", "text": ""}]},
        {"max_source_bytes": 0},
    ],
)
def test_invalid_prompt_config_is_rejected(config):
    with pytest.raises(ValidationError):
        Config.model_validate(config)


async def test_custom_composer_can_be_opaque_and_output_is_detached():
    class Custom:
        name, version = "custom", "1"

        async def compose(self, request, recorder):
            self.output = request.model_copy(update={"system": "custom output"})
            recorder.opaque(self.output, "External builder does not report its internal steps.")
            return self.output

    custom = Custom()
    original = ModelRequest(
        model_alias="demo",
        tools=(ToolDefinition(name="a", description="a", parameters={"type": "object"}),),
    )
    prepared = await PromptService(custom).prepare(original, capture=True)
    custom.output.tools[0].parameters["type"] = "array"
    prepared.request.tools[0].parameters["type"] = "number"
    report = inspected(prepared)
    assert report.build.provenance == "opaque" and report.build.placements[0].target == ""
    assert original.tools[0].parameters["type"] == "object"
    assert report.snapshot is not None
    assert content(report, report.snapshot.request)["tools"][0]["parameters"]["type"] == "object"


async def test_unattributed_custom_output_is_rejected_before_it_can_be_sent():
    class Incomplete:
        name, version = "custom", "1"

        async def compose(self, request, recorder):
            fragment = recorder.source("part", "hello", kind="extension", producer=self.name)
            recorder.place(fragment, "/system", start=0, end=5)
            return request.model_copy(update={"system": "hello untracked"})

    with pytest.raises(PromptBuildFailed) as failure:
        await PromptService(Incomplete()).prepare(ModelRequest(model_alias="demo"))
    assert "cover" in inspected(failure.value).build.error


async def test_file_read_is_settled_before_repeated_cancellation_even_when_io_fails(monkeypatch):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def read(path, limit):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=3):
            raise AssertionError("test failed to release file read")
        raise OSError("private-io-diagnostic")

    monkeypatch.setattr("my_own_agent.prompts.composer.read_text", read)
    task = asyncio.create_task(
        service(sections=(Section(key="file", path="x"),)).prepare(
            ModelRequest(model_alias="demo"),
        )
    )
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(PromptBuildCancelled) as failure:
                await task
        report = inspected(failure.value)
        assert report.build.status == "cancelled" and report.snapshot is None
        assert "private-io-diagnostic" not in failure.value.inspection_json
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_agent_rebuilds_per_request_and_preview_does_not_change_history(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text("before")

    async def update(args):
        path.write_text("after")
        return "4"

    agent, adapter, store, _ = make_agent(
        [
            [request_tools(call())],
            [ModelDone(Message(role="assistant", content="done"), "stop")],
        ],
        handler=update,
    )
    agent.prompts = service(cwd=str(tmp_path), sections=(Section(key="file", path="rules.md"),))
    agent.capture_prompts = True
    preview = await agent.inspect_prompt("preview", "a")
    assert preview.request.messages[-1].content == "preview"
    assert await store.load("a") == [] and adapter.requests == []
    events = [event async for event in agent.run("run", "a")]
    reports = [inspected(event) for event in events if isinstance(event, PromptBuilt)]
    assert len(reports) == len(adapter.requests) == 2
    assert adapter.requests[0].system.startswith("system\n\nbefore\n\nRuntime state")
    assert adapter.requests[1].system.startswith("system\n\nafter\n\nRuntime state")
    assert adapter.requests[1].messages[-1].content == "4"
    assert len({r.build.request_id for r in reports}) == 2
    for report, request in zip(reports, adapter.requests, strict=True):
        assert content(report, report.snapshot.request) == request.model_dump(mode="json")


async def test_prompt_failure_stops_agent_before_model_or_tools(tmp_path):
    agent, adapter, store, executions = make_agent([])
    agent.prompts = service(cwd=str(tmp_path), sections=(Section(key="file", path="missing"),))
    events = [event async for event in agent.run("run")]
    assert isinstance(events[0], PromptBuilt) and inspected(events[0]).build.status == "failed"
    assert isinstance(events[-1], ModelError) and events[-1].code == "prompt_build_failed"
    assert adapter.requests == [] and executions == []
    assert [m.role for m in await store.load("default")] == ["user"]


def test_cli_inspection_needs_no_credentials_or_model_execution():
    result = invoke("--config", "agent.example.toml", "--inspect-prompt", "--prompt", "你好")
    assert result.returncode == 0, result.stderr
    report = PromptInspection.model_validate_json(result.stdout)
    assert report.snapshot is not None
    assert content(report, report.snapshot.request)["messages"][-1]["content"] == "你好"
    assert "工作助手" in content(report, report.snapshot.request)["system"]
    assert content(report, report.snapshot.request)["runtime"]["timezone"] == "Asia/Shanghai"
    assert content(report, report.snapshot.request)["runtime"]["user_submitted_at"] is None
    assert "missing_credential" not in result.stderr and "[tool]" not in result.stderr


def test_cli_show_prompt_preserves_normal_tool_cycle():
    result = invoke("--config", "examples/demo.toml", "--prompt", "time", "--show-prompt")
    assert result.returncode == 0, result.stderr
    assert "Offline demo" in result.stdout
    assert result.stderr.count('"type": "prompt.inspection"') == 2
    assert "[tool] current_time" in result.stderr


def test_generated_schema_matches_source():
    schema = json.loads(Path("docs/schemas/prompt-inspection.v1.schema.json").read_text())
    assert schema == PromptInspection.model_json_schema()


@pytest.mark.parametrize("opaque", [False, True])
async def test_provenance_distinguishes_json_boolean_from_number(opaque):
    class Changed:
        name, version = "custom", "1"

        async def compose(self, request, recorder):
            if opaque:
                recorder.opaque(request, "External output")
            else:
                fragment = recorder.source(
                    "tool.a",
                    request.tools[0].model_dump(mode="json"),
                    kind="tool",
                    producer=self.name,
                )
                recorder.place(fragment, "/tools/0")
            request.tools[0].parameters["default"] = 1
            return request

    with pytest.raises(PromptBuildFailed):
        await PromptService(Changed()).prepare(
            ModelRequest(
                model_alias="demo",
                tools=(ToolDefinition(name="a", description="a", parameters={"default": True}),),
            )
        )


@pytest.mark.parametrize(
    "view",
    [
        {
            "ref": {"content_id": "x", "media_type": "text/plain", "capture_state": "not_captured"},
        },
        {
            "ref": {"content_id": "x", "media_type": "text/plain", "capture_state": "not_captured"},
            "state": "available",
            "inline": "example",
        },
        {
            "ref": {"content_id": "x", "media_type": "text/plain", "capture_state": "not_captured"},
            "state": "not_captured",
            "inline": "secret",
        },
        {
            "ref": {"content_id": "x", "media_type": "text/plain", "capture_state": "captured"},
            "state": "available",
            "inline": "example",
        },
    ],
)
def test_schema_and_python_both_reject_invalid_content_states(view):
    adapter = TypeAdapter[ContentView](ContentView)
    with pytest.raises(ValidationError):
        adapter.validate_python(view)
    with pytest.raises(SchemaError):
        validate(view, adapter.json_schema())
