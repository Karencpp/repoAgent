"""Compare a direct ReAct loop with the current LangGraph orchestration."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import statistics
import time
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

from repo_agent.llm import (
    StructuredDecisionClient,
    StructuredPlanner,
    StructuredReflector,
    structured_client_from_env,
)
from repo_agent.llm.contracts import (
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
    StructuredJSONClient,
    StructuredJSONRequest,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.react import ReActConfig, ReActExecutor, StructuredDecisionModel
from repo_agent.tools import LocalRepositoryTools, ToolErrorKind, ToolResult, build_repository_tool_registry
from repo_agent.workflow import (
    EvidenceBasedDiagnoseEvaluator,
    RepoAgentWorkflow,
    StepExecution,
    StepExecutionRequest,
    WorkflowConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
READ_ONLY_TOOLS = ("inspect_python", "list_files", "read_file_range", "search_code")
TRANSIENT_ERRORS = (LLMRateLimitError, LLMTimeoutError, LLMTransportError)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class MeteredClient:
    def __init__(self, delegate: StructuredJSONClient) -> None:
        self.delegate = delegate
        self.calls = 0
        self.attempts = 0

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        self.calls += 1
        for attempt in range(1, 4):
            self.attempts += 1
            try:
                return self.delegate.generate_json(request)
            except TRANSIENT_ERRORS:
                if attempt == 3:
                    raise
                time.sleep(2.0 * attempt)
        raise AssertionError("retry loop ended unexpectedly")


class BudgetedRegistry:
    """Keep a true task-level tool budget across all LangGraph steps."""

    def __init__(self, delegate: Any, limit: int) -> None:
        self.delegate = delegate
        self.limit = limit
        self.calls = 0
        self.denied_calls = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.calls)

    def model_tools(self, allowed_tools: Iterable[str] | None = None) -> Any:
        return self.delegate.model_tools(allowed_tools)

    def dispatch(
        self,
        tool_name: str,
        raw_arguments: Mapping[str, Any],
        *,
        allowed_tools: Iterable[str] | None = None,
    ) -> Any:
        if self.calls >= self.limit:
            self.denied_calls += 1
            return ToolResult.failure(
                ToolErrorKind.PERMISSION_DENIED,
                "任务级工具调用预算已耗尽",
            )
        self.calls += 1
        return self.delegate.dispatch(
            tool_name,
            raw_arguments,
            allowed_tools=allowed_tools,
        )


class BudgetedStepExecutor:
    def __init__(self, decision_model: Any, registry: BudgetedRegistry) -> None:
        self.decision_model = decision_model
        self.registry = registry

    def execute(self, request: StepExecutionRequest) -> StepExecution:
        previous = "\n".join(
            f"- {result.step_id}: {result.summary}" for result in request.previous_results
        )
        reflection = (
            request.latest_reflection.corrective_action
            if request.latest_reflection is not None
            else "无"
        )
        instructions = (
            f"总目标：{request.user_goal}\n当前步骤：{request.step.goal}\n"
            f"预期证据：{'；'.join(request.step.expected_evidence)}\n"
            f"已有结果：{previous or '无'}\n最近修正：{reflection}\n"
            "只完成当前步骤；结论必须来自成功的只读工具观察并引用相关文件。"
        )
        remaining = self.registry.remaining
        react = ReActExecutor(
            self.decision_model,
            self.registry,
            config=ReActConfig(
                max_iterations=max(2, remaining + 2),
                max_tool_calls=remaining,
                max_consecutive_tool_errors=2,
                max_identical_tool_calls=1,
            ),
        )
        result = react.run(
            request.step.goal,
            system_instructions=instructions,
            allowed_tools=request.step.allowed_tools,
        )
        return StepExecution.from_react_result(request.step.id, result).model_copy(
            update={"execution_key": request.execution_key}
        )


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def contains_any(text: str, terms: list[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(" ".join(term.casefold().split()) in normalized for term in terms)


def collect_paths(value: Any) -> set[str]:
    payload = jsonable(value)
    paths: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            path = item.get("path")
            if isinstance(path, str):
                paths.add(path.replace("\\", "/"))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(payload)
    return paths


def score(case: Mapping[str, Any], answer: str, paths: set[str], normal: bool) -> dict[str, Any]:
    path_recall = sum(path in paths for path in case["required_paths"]) / len(case["required_paths"])
    facts = [contains_any(answer, list(group)) for group in case["required_fact_groups"]]
    return {
        "normal_completion": normal,
        "path_evidence_recall": path_recall,
        "all_paths": path_recall == 1.0,
        "fact_groups_passed": sum(facts),
        "all_facts": all(facts),
        "task_success": normal and path_recall == 1.0 and all(facts),
    }


def aggregate(records: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    selected = [record for record in records if record["arm"] == arm]
    count = len(selected)
    return {
        "runs": count,
        "task_success": {
            "passed": sum(record["score"]["task_success"] for record in selected),
            "rate": sum(record["score"]["task_success"] for record in selected) / count if count else 0.0,
        },
        "mean_path_evidence_recall": statistics.fmean(record["score"]["path_evidence_recall"] for record in selected) if selected else None,
        "mean_tool_calls": statistics.fmean(record["tool_calls"] for record in selected) if selected else None,
        "mean_model_calls": statistics.fmean(record["model_calls"] for record in selected) if selected else None,
        "mean_duration_ms": statistics.fmean(record["duration_ms"] for record in selected) if selected else None,
        "budget_exhausted_runs": sum(record["budget_exhausted"] for record in selected),
        "replanned_runs": sum(record["replan_count"] > 0 for record in selected),
    }


def write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "evals/benchmarks/orchestration-pilot-12.jsonl")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output/benchmarks/orchestration-ab.json")
    parser.add_argument("--repetitions", type=int, default=int(os.environ.get("ORCHESTRATION_AB_REPETITIONS", "1")))
    parser.add_argument("--case-ids", default=os.environ.get("ORCHESTRATION_AB_CASE_IDS", ""))
    parser.add_argument("--tool-budget", type=int, default=8)
    parser.add_argument("--inter-run-delay-seconds", type=float, default=float(os.environ.get("ORCHESTRATION_AB_DELAY_SECONDS", "5")))
    parser.add_argument("--seed", type=int, default=20260809)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_path = args.dataset.resolve()
    output_path = args.output.resolve()
    all_cases = load_cases(dataset_path)
    requested = [item.strip() for item in args.case_ids.split(",") if item.strip()]
    by_id = {case["case_id"]: case for case in all_cases}
    cases = [by_id[item] for item in requested] if requested else all_cases
    model_name = os.environ.get("GLM_MODEL", "glm-5")
    records: list[dict[str, Any]] = []
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if existing.get("model") == model_name:
            records = list(existing.get("records", []))
    completed = {record["run_key"] for record in records}
    schedule = [(case, repetition, arm) for repetition in range(1, args.repetitions + 1) for case in cases for arm in ("react", "langgraph")]
    random.Random(args.seed).shuffle(schedule)
    base_client = structured_client_from_env("glm")
    metered = MeteredClient(base_client)
    resolver = ProjectContextResolver(ProjectRegistry(output_path.parent / "state/orchestration-ab/projects.json"))
    executed = 0
    try:
        for index, (case, repetition, arm) in enumerate(schedule, start=1):
            run_key = f"{model_name}:{case['case_id']}:{repetition}:{arm}:budget{args.tool_budget}"
            if run_key in completed:
                print(f"[{index}/{len(schedule)}] resume {case['case_id']} {arm}", flush=True)
                continue
            if executed and args.inter_run_delay_seconds:
                print(f"cooldown {args.inter_run_delay_seconds:.0f}s", flush=True)
                time.sleep(args.inter_run_delay_seconds)
            context = resolver.resolve(repo=(PROJECT_ROOT / case["repo"]).resolve())
            base_registry = build_repository_tool_registry(LocalRepositoryTools(context))
            registry = BudgetedRegistry(base_registry, args.tool_budget)
            decision_model = StructuredDecisionModel(
                StructuredDecisionClient(metered),
                max_validation_attempts=2,
            )
            before_calls = metered.calls
            started = time.perf_counter()
            if arm == "react":
                executor = ReActExecutor(
                    decision_model,
                    registry,
                    config=ReActConfig(max_iterations=10, max_tool_calls=args.tool_budget),
                )
                result = executor.run(
                    case["goal"],
                    system_instructions="只读分析目标仓库；结论必须来自工具证据并引用相关文件。",
                    allowed_tools=READ_ONLY_TOOLS,
                )
                answer = result.final_answer or ""
                paths = set()
                for event in result.events:
                    if event.result.ok:
                        paths.update(collect_paths(event.arguments))
                        paths.update(collect_paths(event.result.data))
                normal = result.status == "completed"
                status = result.status
                tool_trace = [jsonable(event) for event in result.events]
                replan_count = 0
                reflection_count = 0
                budget_exhausted = result.status == "budget_exhausted"
            else:
                tools = registry.model_tools(READ_ONLY_TOOLS)
                workflow = RepoAgentWorkflow(
                    StructuredPlanner(metered, tools),
                    BudgetedStepExecutor(decision_model, registry),
                    EvidenceBasedDiagnoseEvaluator(),
                    StructuredReflector(metered),
                    config=WorkflowConfig(max_reflections=1, max_replans=1, recursion_limit=40),
                )
                result = workflow.run(context, case["goal"], mode="diagnose")
                answer = "\n".join(step.summary for step in result.step_results)
                paths = set()
                for step in result.step_results:
                    for observation in step.observations:
                        if observation.result.get("status") == "success":
                            paths.update(collect_paths(observation.arguments))
                            paths.update(collect_paths(observation.result.get("data")))
                normal = result.status == "completed"
                status = result.status
                tool_trace = [jsonable(step.observations) for step in result.step_results]
                replan_count = result.replan_count
                reflection_count = result.reflection_count
                budget_exhausted = any(step.react_status == "budget_exhausted" for step in result.step_results)
            duration_ms = (time.perf_counter() - started) * 1000
            record = {
                "run_key": run_key,
                "case_id": case["case_id"],
                "difficulty": case["difficulty"],
                "repetition": repetition,
                "arm": arm,
                "status": status,
                "answer": answer,
                "observed_paths": sorted(paths),
                "tool_calls": registry.calls,
                "budget_denied_calls": registry.denied_calls,
                "model_calls": metered.calls - before_calls,
                "duration_ms": duration_ms,
                "replan_count": replan_count,
                "reflection_count": reflection_count,
                "budget_exhausted": budget_exhausted,
                "tool_trace": tool_trace,
                "score": score(case, answer, paths, normal),
            }
            records.append(record)
            completed.add(run_key)
            executed += 1
            payload = {
                "benchmark": "orchestration_ab_v2",
                "generated_at": utc_now(),
                "model": model_name,
                "tool_budget": args.tool_budget,
                "requested_repetitions": args.repetitions,
                "selected_case_ids": [case["case_id"] for case in cases],
                "protocol": {
                    "react": "single ReAct loop receiving the full goal",
                    "langgraph": "Plan -> Execute -> Evaluate -> Reflect -> Replan with the same ReAct decision model in Execute",
                    "disabled_modules": ["Skill", "Memory", "MCP", "RAG"],
                    "task_success": "normal completion + all required paths in successful tool evidence + all required fact groups in answer",
                },
                "summary": {name: aggregate(records, name) for name in ("react", "langgraph")},
                "records": records,
            }
            write_report(output_path, payload)
            print(f"[{index}/{len(schedule)}] {case['case_id']} {arm}: success={record['score']['task_success']} calls={record['model_calls']} tools={record['tool_calls']}", flush=True)
    finally:
        close = getattr(base_client, "close", None)
        if callable(close):
            close()
    print(json.dumps({name: aggregate(records, name) for name in ("react", "langgraph")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
