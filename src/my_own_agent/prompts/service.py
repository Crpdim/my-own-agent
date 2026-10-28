"""Prompt composition and immutable inspection output, without storage or model I/O."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import JsonValue

from my_own_agent.contracts.v1.prompt import (
    AvailableContent,
    CapturedContentRef,
    ContentRef,
    ContentView,
    PromptBuild,
    PromptFragment,
    PromptInspection,
    PromptOperation,
    PromptPlacement,
    PromptSnapshot,
    PromptSource,
    PromptSourceKind,
    UncapturedContent,
    UncapturedContentRef,
)
from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import ModelRequest


def json_text(value: JsonValue) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class PromptError(Exception):
    """A prompt failure with a safe, user-facing message."""


class PromptBuildFailed(PromptError):
    def __init__(self, inspection_json: str) -> None:
        super().__init__("Prompt construction failed; inspect the build record.")
        self.inspection_json = inspection_json


class PromptBuildCancelled(asyncio.CancelledError):
    def __init__(self, inspection_json: str) -> None:
        super().__init__()
        self.inspection_json = inspection_json


@dataclass(frozen=True)
class PreparedPrompt:
    request: ModelRequest
    inspection_json: str


@dataclass(frozen=True)
class PromptBuilt:
    """One ephemeral build notification. No durable journal or delivery guarantee."""

    inspection_json: str


class PromptRecorder:
    """Owned by one build; final JSON is detached from composer and observer mutations."""

    def __init__(self, composer: str, version: str, capture: bool) -> None:
        self.composer, self.version, self.capture = composer, version, capture
        self.build_id, self.request_id, self.snapshot_id = (uuid4().hex for _ in range(3))
        self.sources: list[PromptSource] = []
        self.fragments: list[PromptFragment] = []
        self.operations: list[PromptOperation] = []
        self.placements: list[PromptPlacement] = []
        self.contents: list[ContentView] = []
        self._values: dict[str, JsonValue] = {}
        self._keys: set[str] = set()
        self._opaque = False

    def _content(self, value: JsonValue, *, captured: bool = True) -> ContentRef:
        text = value if isinstance(value, str) else json_text(value)
        raw = text.encode("utf-8")
        keep = self.capture and captured
        media_type: Literal["text/plain", "application/json"] = (
            "text/plain" if isinstance(value, str) else "application/json"
        )
        if keep:
            ref = CapturedContentRef(content_id=uuid4().hex, media_type=media_type)
            self.contents.append(
                AvailableContent(
                    state="available",
                    ref=ref,
                    inline=json.loads(json_text(value)),
                    size_bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
            return ref
        omitted = UncapturedContentRef(content_id=uuid4().hex, media_type=media_type)
        self.contents.append(UncapturedContent(ref=omitted, state="not_captured"))
        return omitted

    def source(
        self,
        key: str,
        value: JsonValue,
        *,
        kind: PromptSourceKind,
        producer: str,
        locator: str | None = None,
    ) -> str:
        if not key or key in self._keys:
            raise PromptError("Prompt source keys must be non-empty and unique.")
        self._keys.add(key)
        ref = self._content(value)
        source_id, fragment_id = uuid4().hex, uuid4().hex
        self.sources.append(
            PromptSource(
                source_id=source_id,
                producer=producer,
                kind=kind,
                label=key,
                locator=locator,
                status="loaded",
                content=ref,
            )
        )
        self.fragments.append(
            PromptFragment(fragment_id=fragment_id, key=key, source_ids=(source_id,), content=ref)
        )
        self._values[fragment_id] = json.loads(json_text(value))
        return fragment_id

    def skip_source(
        self, key: str, *, locator: str | None, reason: str, failed: bool = False
    ) -> None:
        if not key or key in self._keys:
            raise PromptError("Prompt source keys must be non-empty and unique.")
        self._keys.add(key)
        ref = self._content(None, captured=False)
        source_id = uuid4().hex
        self.sources.append(
            PromptSource(
                source_id=source_id,
                producer=self.composer,
                kind="file" if locator is not None else "config",
                label=key,
                locator=locator,
                status="failed" if failed else "skipped",
                content=ref,
            )
        )
        self._operation("skip", (), reason, source_ids=(source_id,))

    def _operation(
        self,
        action: Literal["include", "skip"],
        ids: tuple[str, ...],
        reason: str,
        *,
        source_ids: tuple[str, ...] = (),
    ) -> None:
        self.operations.append(
            PromptOperation(
                operation_id=uuid4().hex,
                order=len(self.operations) + 1,
                producer=self.composer,
                action=action,
                source_ids=source_ids,
                input_fragment_ids=ids,
                output_fragment_ids=ids if action == "include" else (),
                reason=reason,
            )
        )

    def skip(self, fragment_id: str, reason: str) -> None:
        if fragment_id not in self._values:
            raise PromptError("Skip references an unknown fragment.")
        self._operation("skip", (fragment_id,), reason)

    def transform(self, fragment_id: str, key: str, value: JsonValue, reason: str) -> str:
        """Record a derived value and its input; the composer performs the transformation."""
        if fragment_id not in self._values or not key or key in self._keys:
            raise PromptError("Transformation requires a known input and a unique output key.")
        self._keys.add(key)
        original = next(item for item in self.fragments if item.fragment_id == fragment_id)
        derived_id = uuid4().hex
        ref = self._content(value)
        self.fragments.append(
            PromptFragment(
                fragment_id=derived_id,
                key=key,
                source_ids=original.source_ids,
                content=ref,
            )
        )
        self._values[derived_id] = json.loads(json_text(value))
        self.operations.append(
            PromptOperation(
                operation_id=uuid4().hex,
                order=len(self.operations) + 1,
                producer=self.composer,
                action="transform",
                input_fragment_ids=(fragment_id,),
                output_fragment_ids=(derived_id,),
                reason=reason,
            )
        )
        return derived_id

    def place(
        self,
        fragment_id: str,
        target: str,
        *,
        start: int | None = None,
        end: int | None = None,
        reason: str = "Included in the request.",
    ) -> None:
        if fragment_id not in self._values:
            raise PromptError("Placement references an unknown fragment.")
        self.placements.append(
            PromptPlacement(
                fragment_id=fragment_id,
                snapshot_id=self.snapshot_id,
                target=target,
                start=start,
                end=end,
            )
        )
        self._operation("include", (fragment_id,), reason)

    def opaque(self, request: ModelRequest, reason: str) -> None:
        """Attribute the complete output to the custom composer, without inventing its internals."""
        if self.sources or self.placements:
            raise PromptError("Opaque attribution must be the build's only attribution.")
        self._opaque = True
        fragment = self.source(
            "composer.output",
            request.model_dump(mode="json"),
            kind="extension",
            producer=self.composer,
        )
        self.place(fragment, "", reason=reason)

    def _validate(self, request: ModelRequest) -> None:
        payload = request.model_dump(mode="json")
        if self._opaque:
            if len(self.placements) != 1 or json_text(
                self._values[self.placements[0].fragment_id]
            ) != json_text(payload):
                raise PromptError("Opaque provenance does not match the resulting request.")
            return
        whole = {f"/messages/{i}": value for i, value in enumerate(payload["messages"])}
        whole.update({f"/tools/{i}": value for i, value in enumerate(payload["tools"])})
        if request.runtime is not None:
            whole["/runtime"] = payload["runtime"]
        ranges: list[tuple[int, int]] = []
        for placement in self.placements:
            value = self._values[placement.fragment_id]
            if placement.target == "/system":
                start, end = placement.start, placement.end
                if start is None or end is None or not 0 <= start <= end <= len(request.system):
                    raise PromptError("Invalid system placement range.")
                if not isinstance(value, str) or value != request.system[start:end]:
                    raise PromptError("System placement does not match its source fragment.")
                ranges.append((start, end))
            elif placement.start is not None or placement.end is not None:
                raise PromptError("Whole-object placements cannot have text offsets.")
            # Python container equality conflates JSON booleans and numbers (True == 1).
            elif placement.target not in whole or json_text(
                whole.pop(placement.target)
            ) != json_text(value):
                raise PromptError("Request placement is missing, duplicated or inaccurate.")
        cursor = 0
        for start, end in sorted(ranges):
            if start != cursor:
                raise PromptError("System provenance contains a gap or overlap.")
            cursor = end
        if cursor != len(request.system) or whole:
            raise PromptError("Prompt provenance must cover system, messages, tools and runtime.")

    def finish(
        self,
        status: Literal["succeeded", "failed", "cancelled"],
        request: ModelRequest | None = None,
        error: str | None = None,
    ) -> str:
        snapshot = None
        if request is not None:
            self._validate(request)
            snapshot = PromptSnapshot(
                snapshot_id=self.snapshot_id, request=self._content(request.model_dump(mode="json"))
            )
        build = PromptBuild(
            build_id=self.build_id,
            request_id=self.request_id,
            composer=self.composer,
            composer_version=self.version,
            provenance="opaque" if self._opaque else "tracked",
            status=status,
            sources=tuple(self.sources),
            fragments=tuple(self.fragments),
            operations=tuple(self.operations),
            placements=tuple(self.placements) if snapshot else (),
            snapshot_id=self.snapshot_id if snapshot else None,
            error=error,
        )
        return PromptInspection(
            build=build, snapshot=snapshot, contents=tuple(self.contents)
        ).model_dump_json(indent=2)


class PromptComposer(Protocol):
    name: str
    version: str

    async def compose(self, request: ModelRequest, recorder: PromptRecorder) -> ModelRequest: ...


class PromptService:
    def __init__(self, composer: PromptComposer) -> None:
        self.composer = composer

    async def prepare(self, request: ModelRequest, *, capture: bool = False) -> PreparedPrompt:
        recorder = PromptRecorder(self.composer.name, self.composer.version, capture)
        try:
            result = await self.composer.compose(request.model_copy(deep=True), recorder)
            # Revalidate and detach custom output; do not publish a mutable alias to execution.
            final = ModelRequest.model_validate(result.model_dump(mode="json"))
            report = recorder.finish("succeeded", final)
            return PreparedPrompt(final.model_copy(deep=True), report)
        except asyncio.CancelledError as error:
            raise PromptBuildCancelled(
                recorder.finish("cancelled", error="Build cancelled.")
            ) from error
        except Exception as error:
            detail = str(error) if isinstance(error, PromptError) else "Prompt composer failed."
            raise PromptBuildFailed(recorder.finish("failed", error=detail)) from error


PROMPTS = ServiceKey[PromptService]("prompts")
