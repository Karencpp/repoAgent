"""JSONL dataset loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from .models import ExplainEvalCase, PatchEvalCase, RetrievalEvalCase


CaseT = TypeVar("CaseT", bound=BaseModel)


class EvalDatasetError(ValueError):
    """Raised when an evaluation dataset is invalid before execution."""


def _load_jsonl(path: Path, model_type: type[CaseT]) -> tuple[CaseT, ...]:
    if not path.is_file():
        raise EvalDatasetError(f"dataset does not exist: {path}")
    cases: list[CaseT] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            case = model_type.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise EvalDatasetError(f"{path}:{line_number}: invalid case: {exc}") from exc
        case_id = str(getattr(case, "case_id"))
        if case_id in seen:
            raise EvalDatasetError(f"{path}:{line_number}: duplicate case_id: {case_id}")
        seen.add(case_id)
        cases.append(case)
    if not cases:
        raise EvalDatasetError(f"dataset has no cases: {path}")
    return tuple(cases)


def _validate_fixtures(cases: Iterable[BaseModel], fixtures_root: Path) -> None:
    for case in cases:
        fixture = fixtures_root / str(getattr(case, "repo_fixture"))
        if not fixture.is_dir():
            raise EvalDatasetError(
                f"case {getattr(case, 'case_id')} references missing fixture: {fixture}"
            )


def load_retrieval_cases(
    path: str | Path,
    *,
    fixtures_root: str | Path | None = None,
) -> tuple[RetrievalEvalCase, ...]:
    cases = _load_jsonl(Path(path), RetrievalEvalCase)
    if fixtures_root is not None:
        _validate_fixtures(cases, Path(fixtures_root))
    return cases


def load_explain_cases(
    path: str | Path,
    *,
    fixtures_root: str | Path | None = None,
) -> tuple[ExplainEvalCase, ...]:
    cases = _load_jsonl(Path(path), ExplainEvalCase)
    if fixtures_root is not None:
        _validate_fixtures(cases, Path(fixtures_root))
    return cases


def load_patch_cases(
    path: str | Path,
    *,
    fixtures_root: str | Path | None = None,
) -> tuple[PatchEvalCase, ...]:
    cases = _load_jsonl(Path(path), PatchEvalCase)
    if fixtures_root is not None:
        _validate_fixtures(cases, Path(fixtures_root))
    return cases
