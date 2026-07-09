"""Pydantic schema for Hermes rich message cards."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CardKind = Literal["table", "chart", "metric_grid", "timeline", "status", "receipt", "comparison"]
CardLook = Literal["auto", "default", "dashboard", "minimal", "editorial", "terminal", "receipt", "status"]


class CardStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["auto", "dark", "light"] = "auto"
    look: CardLook = "auto"
    density: Literal["compact", "normal", "roomy"] = "normal"
    accent: str | None = None
    width: int | None = Field(default=None, ge=360, le=1200)
    max_height: int | None = Field(default=None, ge=360, le=2600)


class DeliveryHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: str | None = None
    replace_source: bool = True
    force_document: bool = False
    include_fallback_after: bool = False


class SeriesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    values: list[float]

    @field_validator("values")
    @classmethod
    def values_not_empty(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("chart.series values must not be empty")
        return value


class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bar", "line", "area", "donut", "scatter"]
    labels: list[str] | None = None
    series: list[SeriesSpec]
    x_label: str | None = None
    y_label: str | None = None
    unit: str | None = None
    show_legend: bool = True

    @model_validator(mode="after")
    def labels_match_values(self) -> "ChartSpec":
        if self.labels:
            for idx, series in enumerate(self.series):
                if len(series.values) != len(self.labels):
                    raise ValueError(
                        f"chart.series[{idx}].values length must match chart.labels length"
                    )
        return self


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    value: Any
    status: str | None = None
    note: str | None = None


class MessageCardSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CardKind
    title: str | None = None
    subtitle: str | None = None
    data: dict[str, Any] | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    chart: ChartSpec | None = None
    metrics: list[MetricSpec] | None = None
    events: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] | None = None
    style: CardStyle = Field(default_factory=CardStyle)
    delivery: DeliveryHints = Field(default_factory=DeliveryHints)

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "MessageCardSpec":
        if self.kind in {"table", "comparison"}:
            if not self.columns or not self.rows:
                raise ValueError(f"{self.kind} cards require columns and rows")
        if self.kind == "chart" and self.chart is None:
            raise ValueError("chart cards require chart")
        if self.kind == "metric_grid" and not self.metrics:
            raise ValueError("metric_grid cards require metrics")
        if self.kind == "status" and not (self.rows or self.metrics or self.items):
            raise ValueError("status cards require rows, metrics, or items")
        if self.kind == "timeline" and not (self.events or self.rows):
            raise ValueError("timeline cards require events or rows")
        if self.kind == "receipt" and not (self.items or self.rows):
            raise ValueError("receipt cards require items or rows")
        return self


class CardQAResult(BaseModel):
    ok: bool
    warnings: list[str] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
    bytes: int | None = None


class CardRenderResult(BaseModel):
    ok: bool
    image_path: str | None = None
    svg_path: str | None = None
    fallback_markdown: str
    alt: str
    warnings: list[str] = Field(default_factory=list)
    repairs: list[str] = Field(default_factory=list)
    error: str | None = None
    correct_example: dict[str, Any] | None = None
    qa: CardQAResult | None = None

    @property
    def media(self) -> str | None:
        return f"MEDIA:{self.image_path}" if self.ok and self.image_path else None
