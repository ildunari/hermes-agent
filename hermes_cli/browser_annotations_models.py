"""Strict, versioned models for backend-owned browser annotations.

The annotation store deliberately contains no comment or chat message bodies.  It
keeps semantic anchor/capture evidence and stable lineage references; the normal
Hermes session store remains authoritative for message content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Normalized = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        alias_generator=lambda name: "".join([
            name.split("_")[0],
            *[part.title() for part in name.split("_")[1:]],
        ]),
    )


class AuthorRef(StrictModel):
    kind: Literal["human", "agent", "system"]
    actor_id: NonEmpty | None = None
    profile_id: NonEmpty | None = None
    display_name: NonEmpty


class ThreadRef(StrictModel):
    """Body-free reference to the independent annotation lineage."""

    session_lineage_id: NonEmpty
    branch_id: NonEmpty
    ordered_message_ids: tuple[int, ...] = ()


class AnnotationScope(StrictModel):
    profile_id: NonEmpty
    browser_workspace_id: NonEmpty
    tab_id: NonEmpty
    document_generation_id: NonEmpty
    session_lineage_id: NonEmpty | None = None
    requested_url: NonEmpty
    committed_url: NonEmpty
    canonical_url: NonEmpty | None = None
    origin: NonEmpty


class ElementFingerprint(StrictModel):
    namespace: NonEmpty | None = None
    tag: NonEmpty
    role: NonEmpty | None = None
    accessible_name_digest: Digest | None = None
    stable_attributes: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)
    text_digest: Digest | None = None
    ancestor_digests: tuple[Digest, ...] = ()
    sibling_ordinal: int | None = Field(default=None, ge=0)
    selector_hints: tuple[NonEmpty, ...] = ()
    shadow_host_digests: tuple[Digest, ...] = ()


class FrameDescriptor(StrictModel):
    origin: NonEmpty
    committed_url_evidence: NonEmpty
    frame_name: str | None = None
    embedding_element_digest: Digest | None = None
    opaque_origin: bool = False
    frame_id_hint: str | None = None


class QuoteSelector(StrictModel):
    exact: NonEmpty
    prefix: str = Field(default="", max_length=256)
    suffix: str = Field(default="", max_length=256)


class ElementAnchor(StrictModel):
    type: Literal["element"] = "element"
    frame_path: tuple[FrameDescriptor, ...]
    fingerprint: ElementFingerprint


class TextAnchor(StrictModel):
    type: Literal["text"] = "text"
    frame_path: tuple[FrameDescriptor, ...]
    quote: QuoteSelector
    start_element: ElementFingerprint
    end_element: ElementFingerprint
    start_utf16_offset: int = Field(ge=0)
    end_utf16_offset: int = Field(ge=0)
    direction: Literal["forward", "backward"]
    text_normalization_version: Literal[1] = 1

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "TextAnchor":
        if self.start_utf16_offset > self.end_utf16_offset:
            raise ValueError("text anchor start offset must not exceed end offset")
        return self


class NormalizedRect(StrictModel):
    x: Normalized
    y: Normalized
    width: Normalized
    height: Normalized

    @model_validator(mode="after")
    def contained(self) -> "NormalizedRect":
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError(
                "normalized rectangle must fit within its coordinate space"
            )
        return self


class CssRect(StrictModel):
    x: float
    y: float
    width: Annotated[float, Field(ge=0.0)]
    height: Annotated[float, Field(ge=0.0)]


class RegionAnchor(StrictModel):
    type: Literal["region"] = "region"
    frame_path: tuple[FrameDescriptor, ...]
    normalized_crop_rect: NormalizedRect
    document_css_rect: CssRect
    containing_element: ElementFingerprint | None = None


class StrokePoint(StrictModel):
    x: Normalized
    y: Normalized


class DrawingStroke(StrictModel):
    points: tuple[StrokePoint, ...] = Field(min_length=2, max_length=4096)
    tool: Literal["pen", "highlighter", "arrow"]
    color: NonEmpty
    width: Annotated[float, Field(gt=0.0, le=1.0)]


class DrawingAnchor(StrictModel):
    type: Literal["drawing"] = "drawing"
    frame_path: tuple[FrameDescriptor, ...]
    region: RegionAnchor
    strokes: tuple[DrawingStroke, ...] = Field(min_length=1, max_length=256)


class AgentMarkerAnchor(StrictModel):
    type: Literal["agent-marker"] = "agent-marker"
    frame_path: tuple[FrameDescriptor, ...]
    snapshot_id: NonEmpty
    ref: NonEmpty
    number: int = Field(ge=1)
    promoted_element: ElementFingerprint


class PageAnchor(StrictModel):
    type: Literal["page"] = "page"
    frame_path: tuple[FrameDescriptor, ...] = ()


AnnotationAnchor = Annotated[
    ElementAnchor
    | TextAnchor
    | RegionAnchor
    | DrawingAnchor
    | AgentMarkerAnchor
    | PageAnchor,
    Field(discriminator="type"),
]


class BlobRef(StrictModel):
    sha256: Digest
    size_bytes: int = Field(ge=0)
    media_type: NonEmpty
    privacy_redacted: bool = False


class RetainedBlob(StrictModel):
    """Repository projection for one capture's content-addressed bytes."""

    capture_id: NonEmpty
    blob: BlobRef
    purpose: Literal["screenshot", "redacted-crop"]
    retention_state: Literal["retained", "missing", "expired"]
    available: bool


class AnnotationDeleteResult(StrictModel):
    """Durable progress for deletion of the current annotation repository slice."""

    delete_job_id: NonEmpty
    annotation_id: NonEmpty
    state: Literal["deleting", "complete", "failed"]
    phase: Literal["metadata", "blob_cleanup", "complete"]
    blob_states: dict[
        Digest, Literal["pending", "deleted", "missing", "preserved", "failed"]
    ]
    error: str | None = None


class ViewportEvidence(StrictModel):
    css_width: Annotated[float, Field(gt=0.0)]
    css_height: Annotated[float, Field(gt=0.0)]
    layout_scroll_x: float
    layout_scroll_y: float
    visual_offset_x: float
    visual_offset_y: float
    visual_scale: Annotated[float, Field(gt=0.0)]
    page_zoom_factor: Annotated[float, Field(gt=0.0)]
    device_scale_factor: Annotated[float, Field(gt=0.0)]


class CaptureEvidence(StrictModel):
    blob: BlobRef | None = None
    image_pixel_width: int | None = Field(default=None, ge=1)
    image_pixel_height: int | None = Field(default=None, ge=1)
    crop_rect: CssRect | None = None
    viewport: ViewportEvidence
    guest_bounds: CssRect
    top_frame_id: NonEmpty
    target_frame_id: NonEmpty
    frame_to_root_rects: tuple[CssRect, ...] = ()
    committed_url: NonEmpty
    captured_at: datetime
    document_generation_id: NonEmpty
    observed_target_rects: tuple[CssRect, ...] = ()
    visible: bool
    occluded: bool | None = None

    @model_validator(mode="after")
    def image_dimensions_are_paired(self) -> "CaptureEvidence":
        if (self.image_pixel_width is None) != (self.image_pixel_height is None):
            raise ValueError("capture image dimensions must be supplied together")
        return self


class AnnotationRecordV1(StrictModel):
    schema_version: Literal[1] = 1
    annotation_id: NonEmpty
    kind: Literal["agent-marker", "element", "text", "region", "drawing", "comment"]
    scope: AnnotationScope
    anchor: AnnotationAnchor
    capture: CaptureEvidence
    author: AuthorRef
    thread: ThreadRef | None = None
    status: Literal["open", "resolved", "dismissed"] = "open"
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    current_anchor_revision_id: NonEmpty
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def record_invariants(self) -> "AnnotationRecordV1":
        if self.capture.document_generation_id != self.scope.document_generation_id:
            raise ValueError("capture and scope document generations must match")
        if self.capture.committed_url != self.scope.committed_url:
            raise ValueError("capture and scope committed URLs must match")
        if self.status == "resolved" and self.resolved_at is None:
            raise ValueError("resolved annotations require resolvedAt")
        if self.status != "resolved" and self.resolved_at is not None:
            raise ValueError("only resolved annotations may carry resolvedAt")
        if self.updated_at < self.created_at:
            raise ValueError("updatedAt must not precede createdAt")
        return self


class AnchorRevision(StrictModel):
    anchor_revision_id: NonEmpty
    annotation_id: NonEmpty
    revision: int = Field(ge=1)
    anchor: AnnotationAnchor
    capture: CaptureEvidence
    changed_by: AuthorRef
    changed_at: datetime
    reason: Literal["created", "reattached", "imported"]
    previous_anchor_digest: Digest | None = None


class AnnotationExportBlob(StrictModel):
    """One immutable capture reference and its explicitly optional payload."""

    capture_id: NonEmpty
    blob: BlobRef
    purpose: Literal["screenshot", "redacted-crop"]
    retention_state: Literal["retained", "missing", "expired"]
    payload_state: Literal["included", "omitted", "missing"]
    payload_base64: str | None = None

    @model_validator(mode="after")
    def payload_declaration_is_consistent(self) -> "AnnotationExportBlob":
        if (self.payload_state == "included") != (self.payload_base64 is not None):
            raise ValueError(
                "included capture payloads require payloadBase64 exclusively"
            )
        if self.payload_state == "missing" and self.retention_state == "retained":
            raise ValueError("missing payloads cannot declare retained bytes")
        if (
            self.payload_state in {"included", "omitted"}
            and self.retention_state != "retained"
        ):
            raise ValueError("included or omitted payloads require retained bytes")
        return self


class AnnotationExportBundleV1(StrictModel):
    """Frozen metadata-first bundle for the current annotation repository only.

    Dedicated annotation lineage/session bodies are deliberately excluded. Their
    future backend owns its own frozen session-export projection.
    """

    bundle_version: Literal[1] = 1
    kind: Literal["hermes-browser-annotation"] = "hermes-browser-annotation"
    repository_scope: Literal["annotation-metadata-and-captures"] = (
        "annotation-metadata-and-captures"
    )
    includes_thread_bodies: Literal[False] = False
    profile_id: NonEmpty
    annotation: AnnotationRecordV1
    anchor_revisions: tuple[AnchorRevision, ...]
    capture_blobs: tuple[AnnotationExportBlob, ...] = ()


class AnnotationImportResult(StrictModel):
    annotation_id: NonEmpty
    metadata_committed: bool
    exact_retry: bool
    payloads_retained: int = Field(ge=0)
    payloads_omitted: int = Field(ge=0)
    payloads_missing: int = Field(ge=0)


class AnnotationProjection(StrictModel):
    record: AnnotationRecordV1
    orphaned: bool
    read_only: bool


def canonical_json(value: BaseModel | dict | list) -> str:
    """Return the byte-stable JSON form used by repository digests and export."""

    if isinstance(value, BaseModel):
        data = value.model_dump(mode="json", by_alias=True)
    else:
        data = value
    return json.dumps(
        data,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: BaseModel | dict | list) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
