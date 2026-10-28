"""Ephemeral prompt inspection views; content is included only on explicit capture."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from my_own_agent.llm.types import DataModel


class ContentRef(DataModel):
    content_id: str
    media_type: Literal["text/plain", "application/json"]
    capture_state: Literal["captured", "not_captured"]


class CapturedContentRef(ContentRef):
    capture_state: Literal["captured"] = "captured"


class UncapturedContentRef(ContentRef):
    capture_state: Literal["not_captured"] = "not_captured"


class AvailableContent(DataModel):
    ref: CapturedContentRef
    state: Literal["available"]
    inline: JsonValue
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UncapturedContent(DataModel):
    ref: UncapturedContentRef
    state: Literal["not_captured"]
    inline: None = None
    size_bytes: None = None
    sha256: None = None


type ContentView = Annotated[AvailableContent | UncapturedContent, Field(discriminator="state")]


type PromptSourceKind = Literal[
    "config", "file", "history", "tool", "composer", "runtime", "extension"
]


class PromptSource(DataModel):
    source_id: str
    producer: str
    kind: PromptSourceKind
    label: str
    locator: str | None = None
    status: Literal["loaded", "skipped", "failed"]
    content: ContentRef


class PromptFragment(DataModel):
    fragment_id: str
    key: str
    source_ids: tuple[str, ...]
    content: ContentRef


class PromptOperation(DataModel):
    operation_id: str
    order: int = Field(ge=1)
    producer: str
    action: Literal["include", "skip", "transform"]
    source_ids: tuple[str, ...] = ()
    input_fragment_ids: tuple[str, ...]
    output_fragment_ids: tuple[str, ...]
    reason: str


class PromptPlacement(DataModel):
    fragment_id: str
    snapshot_id: str
    target: str
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class PromptBuild(DataModel):
    build_id: str
    request_id: str
    composer: str
    composer_version: str
    provenance: Literal["tracked", "opaque"]
    status: Literal["succeeded", "failed", "cancelled"]
    sources: tuple[PromptSource, ...]
    fragments: tuple[PromptFragment, ...]
    operations: tuple[PromptOperation, ...]
    placements: tuple[PromptPlacement, ...]
    snapshot_id: str | None = None
    error: str | None = None


class PromptSnapshot(DataModel):
    snapshot_id: str
    stage: Literal["assembled"] = "assembled"
    request: ContentRef


class PromptInspection(DataModel):
    schema_version: Literal[1] = 1
    type: Literal["prompt.inspection"] = "prompt.inspection"
    build: PromptBuild
    snapshot: PromptSnapshot | None = None
    contents: tuple[ContentView, ...]
