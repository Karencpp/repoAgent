"""Measure LangGraph checkpoint recovery across persisted node boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
from typing import Any

from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.workflow import (
    EvaluationResult,
    ExecutionPlan,
    PlanStep,
    ReflectionResult,
    SQLiteWorkflowRuntime,
    ScriptedEvaluator,
    ScriptedPlanner,
    ScriptedReflector,
    ScriptedStepExecutor,
    StepExecution,
)


NODES = ("plan", "execute_step", "evaluate", "reflect", "replan", "report")


def _plan(step_id: str) -> ExecutionPlan:
    return ExecutionPlan(
        rationale=f"Complete and verify {step_id}",
        steps=(
            PlanStep(
                id=step_id,
                goal=f"Complete {step_id}",
                expected_evidence=("verified result",),
                allowed_tools=("search_code",),
            ),
        ),
    )


def _completed(step_id: str) -> StepExecution:
    return StepExecution(
        step_id=step_id,
        status="completed",
        summary=f"{step_id} completed",
        react_status="completed",
        stop_reason="verified scripted execution",
        iterations=1,
        tool_calls=1,
    )


def _passed() -> EvaluationResult:
    return EvaluationResult(
        passed=True,
        summary="objective evidence passed",
        evidence=("src/service.py:1",),
    )


def _rejected() -> EvaluationResult:
    return EvaluationResult(
        passed=False,
        summary="independent verification is missing",
        issues=("missing verification",),
    )


def _replan_reflection() -> ReflectionResult:
    return ReflectionResult(
        failure_cause="the original plan omitted independent verification",
        corrective_action="append a verification step",
        should_replan=True,
    )


def _adapters(node: str, phase: str):
    """Return only the scripted responses reachable in the selected phase."""

    if phase == "before":
        initial = () if node == "plan" else (_plan("locate"),)
        executions = (
            (_completed("locate"),)
            if node in {"evaluate", "reflect", "replan", "report"}
            else ()
        )
        evaluations = (
            (_passed(),)
            if node == "report"
            else ((_rejected(),) if node in {"reflect", "replan"} else ())
        )
        reflections = (_replan_reflection(),) if node == "replan" else ()
        replans = ()
    else:
        initial = (_plan("locate"),) if node == "plan" else ()
        executions = (
            (_completed("locate"),)
            if node in {"plan", "execute_step"}
            else ((_completed("verify"),) if node in {"reflect", "replan"} else ())
        )
        evaluations = (_passed(),) if node != "report" else ()
        reflections = (_replan_reflection(),) if node == "reflect" else ()
        replans = (_plan("verify"),) if node in {"reflect", "replan"} else ()
    return (
        ScriptedPlanner(initial, replans=replans),
        ScriptedStepExecutor(executions),
        ScriptedEvaluator(evaluations),
        ScriptedReflector(reflections),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def run_benchmark(workspace: Path) -> dict[str, Any]:
    repo = workspace / "target-repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "service.py").write_text(
        "class Service:\n    pass\n", encoding="utf-8"
    )
    context = ProjectContextResolver(
        ProjectRegistry(workspace / "projects.json")
    ).resolve(repo=repo)

    details = []
    for node in NODES:
        database = workspace / f"checkpoint-{node}.sqlite3"
        database.unlink(missing_ok=True)
        before = _adapters(node, "before")
        with SQLiteWorkflowRuntime(
            database,
            *before,
            interrupt_before=(node,),
        ) as runtime:
            interrupted = runtime.start(
                context,
                f"checkpoint recovery at {node}",
                thread_id=f"recovery-{node}",
                run_id=f"run-{node}",
            )
            snapshot = runtime.latest(context, thread_id=f"recovery-{node}")

        after = _adapters(node, "after")
        started = time.perf_counter()
        with SQLiteWorkflowRuntime(database, *after) as runtime:
            resumed = runtime.resume(context, thread_id=f"recovery-{node}")
        recovery_ms = (time.perf_counter() - started) * 1_000

        requests = [*before[1].requests, *after[1].requests]
        execution_keys = [request.execution_key for request in requests]
        duplicate_keys = len(execution_keys) - len(set(execution_keys))
        success = (
            interrupted.status == "interrupted"
            and snapshot.next_nodes == (node,)
            and resumed.status == "completed"
            and duplicate_keys == 0
        )
        details.append(
            {
                "node": node,
                "success": success,
                "interrupted_status": interrupted.status,
                "checkpoint_next_nodes": snapshot.next_nodes,
                "resumed_status": resumed.status,
                "recovery_ms": recovery_ms,
                "step_execution_calls_before_restart": len(before[1].requests),
                "step_execution_calls_after_restart": len(after[1].requests),
                "execution_keys": execution_keys,
                "duplicate_execution_keys": duplicate_keys,
            }
        )

    durations = [item["recovery_ms"] for item in details]
    return {
        "schema": "repo-agent-checkpoint-recovery-v1",
        "case_count": len(details),
        "recovery_success_rate": sum(item["success"] for item in details)
        / len(details),
        "duplicate_execution_keys": sum(
            item["duplicate_execution_keys"] for item in details
        ),
        "recovery_latency_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "mean": statistics.fmean(durations),
        },
        "scope": "controlled service restart at persisted node boundaries",
        "cases": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    state_dir = arguments.output.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    report = run_benchmark(state_dir / "checkpoint-recovery-workspace")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["recovery_success_rate"] == 1.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
