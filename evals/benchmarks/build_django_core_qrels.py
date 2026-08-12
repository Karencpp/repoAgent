"""Build a reproducible, stratified retrieval dataset for Django core source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any


EXPECTED_COMMIT = "c9eb16a87e60c305fb3651459639f647cce498db"


def _symbols(repo: Path) -> tuple[dict[str, Any], ...]:
    candidates = []
    name_counts: dict[str, int] = {}
    for path in sorted(repo.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        relative = path.relative_to(repo).as_posix()
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") or not 4 <= len(node.name) <= 48:
                continue
            item = {
                "name": node.name,
                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                "path": relative,
                "line": node.lineno,
                "subsystem": relative.split("/", 1)[0],
            }
            candidates.append(item)
            name_counts[node.name] = name_counts.get(node.name, 0) + 1
    return tuple(item for item in candidates if name_counts[item["name"]] == 1)


def _stable_order(item: dict[str, Any]) -> str:
    payload = f"{EXPECTED_COMMIT}:{item['name']}:{item['path']}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _round_robin_symbols(
    symbols: tuple[dict[str, Any], ...], count: int
) -> tuple[dict[str, Any], ...]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in symbols:
        groups.setdefault(item["subsystem"], []).append(item)
    for values in groups.values():
        values.sort(key=_stable_order)
    selected = []
    group_names = sorted(groups)
    while len(selected) < count:
        progressed = False
        for group in group_names:
            if groups[group]:
                selected.append(groups[group].pop(0))
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            raise ValueError(f"Only found {len(selected)} eligible unique symbols")
    return tuple(selected)


def _semantic_cases(path: Path, repo: Path) -> list[dict[str, Any]]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        relevant = [
            item.removeprefix("django/") for item in raw["relevant_paths"]
        ]
        if not all((repo / item).is_file() for item in relevant):
            raise ValueError(f"Invalid semantic qrels: {raw['case_id']}")
        cases.append(
            {
                "case_id": f"semantic-{raw['case_id']}",
                "category": "semantic",
                "query": raw["query"],
                "relevant_paths": relevant,
            }
        )
    return cases


def build_dataset(repo: Path, semantic_path: Path) -> tuple[dict[str, Any], ...]:
    symbols = _symbols(repo)
    exact_symbols = _round_robin_symbols(symbols, 60)
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(exact_symbols, 1):
        cases.append(
            {
                "case_id": f"exact-symbol-{index:03d}",
                "category": "exact_symbol",
                "query": (
                    f"Where is the Django {item['kind']} {item['name']} implemented?"
                ),
                "relevant_paths": [item["path"]],
                "relevant_symbols": [item["name"]],
            }
        )

    cases.extend(_semantic_cases(semantic_path, repo))

    by_subsystem: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(symbols, key=_stable_order):
        by_subsystem.setdefault(item["subsystem"], []).append(item)
    pairs = []
    for subsystem in sorted(by_subsystem):
        values = by_subsystem[subsystem]
        for left_index, left in enumerate(values):
            right = next(
                (item for item in values[left_index + 1 :] if item["path"] != left["path"]),
                None,
            )
            if right is not None:
                pairs.append((subsystem, left, right))
    pairs.sort(
        key=lambda pair: hashlib.sha256(
            f"{EXPECTED_COMMIT}:{pair[0]}:{pair[1]['name']}:{pair[2]['name']}".encode()
        ).hexdigest()
    )
    if len(pairs) < 30:
        raise ValueError("Not enough cross-file symbol pairs")
    for index, (subsystem, left, right) in enumerate(pairs[:30], 1):
        cases.append(
            {
                "case_id": f"multi-file-{index:03d}",
                "category": "multi_file_symbol",
                "query": (
                    f"In Django's {subsystem} subsystem, locate both "
                    f"{left['name']} and {right['name']} implementations."
                ),
                "relevant_paths": [left["path"], right["path"]],
                "relevant_symbols": [left["name"], right["name"]],
            }
        )

    if len(cases) != 120:
        raise AssertionError(f"Expected 120 cases, got {len(cases)}")
    if len({case["case_id"] for case in cases}) != len(cases):
        raise AssertionError("Duplicate case IDs")
    for case in cases:
        for relative in case["relevant_paths"]:
            if not (repo / relative).is_file():
                raise AssertionError(f"Missing qrel path: {relative}")
    return tuple(cases)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--semantic-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    cases = build_dataset(arguments.repo, arguments.semantic_dataset)
    arguments.output.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    print(json.dumps({"case_count": len(cases), "categories": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
