"""Strict schemas shared by offline evaluation datasets and reports."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvalModel(BaseModel):
    """Common strict Pydantic settings for eval models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _safe_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("paths must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("paths must be normalized relative paths")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("paths cannot contain empty or current-directory parts")
    return value


class LineRange(EvalModel):
    """Expected relevant source line span."""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    _safe_path = field_validator("path")(_safe_relative_path)

    @model_validator(mode="after")
    def validate_order(self) -> "LineRange":
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot be smaller than start_line")
        return self


class RetrievalEvalCase(EvalModel):
    """One repository retrieval evaluation case."""

    case_id: str = Field(min_length=1, max_length=100)
    repo_fixture: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2_000)
    relevant_paths: tuple[str, ...] = Field(min_length=1)
    relevant_symbols: tuple[str, ...] = ()
    relevant_line_ranges: tuple[LineRange, ...] = ()
    top_k: int = Field(default=5, ge=1, le=20)

    _safe_fixture = field_validator("repo_fixture")(_safe_relative_path)
    _safe_paths = field_validator("relevant_paths")(
        lambda value: tuple(_safe_relative_path(path) for path in value)
    )


class ExplainEvalCase(EvalModel):
    """One explain workflow evaluation case."""

    case_id: str = Field(min_length=1, max_length=100)
    repo_fixture: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2_000)
    required_paths: tuple[str, ...] = Field(min_length=1)
    required_claims: tuple[str, ...] = Field(min_length=1)
    forbidden_claims: tuple[str, ...] = ()

    _safe_fixture = field_validator("repo_fixture")(_safe_relative_path)
    _safe_paths = field_validator("required_paths")(
        lambda value: tuple(_safe_relative_path(path) for path in value)
    )


class PatchEvalCase(EvalModel):
    """One patch repair evaluation case."""

    case_id: str = Field(min_length=1, max_length=100)
    repo_fixture: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2_000)
    target_tests: tuple[str, ...] = Field(min_length=1)
    regression_tests: tuple[str, ...] = Field(default=("tests",), min_length=1)
    expected_changed_paths: tuple[str, ...] = Field(min_length=1)
    forbidden_changed_paths: tuple[str, ...] = ()

    _safe_fixture = field_validator("repo_fixture")(_safe_relative_path)
    _safe_expected = field_validator("expected_changed_paths")(
        lambda value: tuple(_safe_relative_path(path) for path in value)
    )
    _safe_forbidden = field_validator("forbidden_changed_paths")(
        lambda value: tuple(_safe_relative_path(path) for path in value)
    )


class RunMetrics(EvalModel):
    """Provider-independent operational metrics for one eval run."""

    duration_ms: int = Field(ge=0)
    llm_requests: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    rag_queries: int | None = Field(default=None, ge=0)
    memory_queries: int | None = Field(default=None, ge=0)
    patch_attempts: int | None = Field(default=None, ge=0)


class EvalCaseResult(EvalModel):
    """Generic case-level result stored in JSON reports."""

    case_id: str
    passed: bool
    metrics: dict[str, float | int | str | bool | None]
    details: dict[str, Any] = Field(default_factory=dict)


class EvalReport(EvalModel):
    """Stable JSON report emitted by each runner."""

    suite: Literal["retrieval", "explain", "patch"]
    dataset: str
    passed: bool
    case_count: int = Field(ge=0)
    metrics: RunMetrics
    cases: tuple[EvalCaseResult, ...]
