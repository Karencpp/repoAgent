"""Benchmark deterministic Skill routing, activation, and script contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from repo_agent.skills import (
    SkillCatalog,
    SkillRouter,
    register_skill_script_tools,
    skill_script_scope,
)
from repo_agent.tools import ToolDefinition, ToolRegistry, ToolResult


BASE_TOOLS = (
    "list_files",
    "search_code",
    "read_file_range",
    "inspect_python",
    "run_pytest",
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in BASE_TOOLS:
        registry.register_json_schema(
            ToolDefinition(
                name=name,
                description=f"benchmark {name}",
                access="read",
                executes_project_code=False,
                requires_explicit_authorization=False,
            ),
            {"type": "object", "additionalProperties": True},
            lambda arguments: ToolResult.success(dict(arguments)),
        )
    return registry


def _load_routing_cases(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _script_cases(skills_root: Path) -> tuple[dict[str, Any], ...]:
    mappings = (
        (
            "diagnose-pytest-failure",
            "classify_pytest_failure",
            skills_root / "diagnose-pytest-failure/tests/classification-cases.json",
            "expected_category",
            "category",
        ),
        (
            "safe-python-refactor",
            "analyze_python_api_change",
            skills_root / "safe-python-refactor/tests/api-change-cases.json",
            "expected_risk_level",
            "risk_level",
        ),
    )
    cases = []
    for skill_name, tool_name, path, expected_key, result_key in mappings:
        for raw in json.loads(path.read_text(encoding="utf-8")):
            cases.append(
                {
                    "skill_name": skill_name,
                    "tool_name": tool_name,
                    "name": raw["name"],
                    "input": raw["input"],
                    "expected": raw[expected_key],
                    "result_key": result_key,
                }
            )
    return tuple(cases)


def run_benchmark(
    skills_root: Path,
    routing_dataset: Path,
    *,
    repetitions: int = 10,
) -> dict[str, Any]:
    catalog = SkillCatalog((skills_root,))
    discovery = catalog.refresh()
    if discovery.diagnostics:
        raise RuntimeError(f"Skill discovery diagnostics: {discovery.diagnostics}")
    router = SkillRouter()
    routing_details = []
    for case in _load_routing_cases(routing_dataset):
        predictions = []
        for _ in range(repetitions):
            matches = router.route(
                case["goal"],
                catalog.descriptors(),
                mode=case["mode"],
                limit=1,
            )
            predictions.append(matches[0].skill.name if matches else None)
        routing_details.append(
            {
                "case_id": case["case_id"],
                "expected_skill": case["expected_skill"],
                "predicted_skill": predictions[0],
                "correct": predictions[0] == case["expected_skill"],
                "consistent": len(set(predictions)) == 1,
            }
        )

    registry = _registry()
    registered_scripts = register_skill_script_tools(catalog, registry)
    activation_details = []
    for descriptor in catalog.descriptors():
        activated = catalog.activate(
            descriptor.name,
            registry,
            runtime_allowed_tools=BASE_TOOLS + registered_scripts,
            mode=descriptor.modes[0],
        )
        activation_details.append(
            {
                "skill_name": descriptor.name,
                "success": bool(activated.instructions)
                and set(activated.effective_tools).issubset(
                    set(descriptor.allowed_tools)
                ),
                "effective_tools": activated.effective_tools,
                "loaded_instruction_resources": len(activated.loaded_resources),
                "content_hash": activated.content_hash,
            }
        )

    script_cases = _script_cases(skills_root)
    script_details = []
    for case in script_cases:
        with skill_script_scope(case["skill_name"]):
            result = registry.dispatch(case["tool_name"], case["input"])
        actual = result.data.get(case["result_key"]) if result.ok else None
        script_details.append(
            {
                "skill_name": case["skill_name"],
                "name": case["name"],
                "success": result.ok and actual == case["expected"],
                "expected": case["expected"],
                "actual": actual,
                "duration_ms": result.metadata.get("duration_ms"),
            }
        )

    denial_details = []
    valid_input_by_tool = {
        case["tool_name"]: case["input"] for case in script_cases
    }
    for descriptor in catalog.descriptors():
        for script in descriptor.scripts:
            result = registry.dispatch(
                script.tool_name,
                valid_input_by_tool[script.tool_name],
            )
            denial_details.append(
                {
                    "skill_name": descriptor.name,
                    "tool_name": script.tool_name,
                    "denied": not result.ok
                    and result.error is not None
                    and result.error.kind.value == "permission_denied",
                }
            )

    return {
        "schema": "repo-agent-skill-system-benchmark-v1",
        "routing": {
            "case_count": len(routing_details),
            "repetitions_per_case": repetitions,
            "accuracy": sum(item["correct"] for item in routing_details)
            / len(routing_details),
            "consistency": sum(item["consistent"] for item in routing_details)
            / len(routing_details),
            "cases": routing_details,
        },
        "activation": {
            "case_count": len(activation_details),
            "success_rate": sum(item["success"] for item in activation_details)
            / len(activation_details),
            "cases": activation_details,
        },
        "script_contracts": {
            "case_count": len(script_details),
            "pass_rate": sum(item["success"] for item in script_details)
            / len(script_details),
            "cases": script_details,
        },
        "unauthorized_script_calls": {
            "case_count": len(denial_details),
            "denial_rate": sum(item["denied"] for item in denial_details)
            / len(denial_details),
            "cases": denial_details,
        },
        "scope": "deterministic Skill control plane; excludes LLM task success",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skills-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    arguments = parser.parse_args()
    report = run_benchmark(
        arguments.skills_root,
        arguments.dataset,
        repetitions=arguments.repetitions,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    passed = all(
        metric == 1.0
        for metric in (
            report["routing"]["accuracy"],
            report["routing"]["consistency"],
            report["activation"]["success_rate"],
            report["script_contracts"]["pass_rate"],
            report["unauthorized_script_calls"]["denial_rate"],
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
