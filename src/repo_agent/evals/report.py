"""Report helpers for CLI output."""

from __future__ import annotations

import json
from pathlib import Path

from .models import EvalReport


def write_report(report: EvalReport, output: str | Path | None = None) -> str:
    """Serialize a report and optionally write it to disk."""

    content = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
    return content
