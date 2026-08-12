"""Measure task quality with the pytest diagnosis Skill enabled and disabled."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import time
from typing import Any, Mapping

from pydantic import BaseModel

from repo_agent.llm import StructuredDecisionClient, structured_client_from_env
from repo_agent.llm.contracts import (
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
    StructuredJSONClient,
    StructuredJSONRequest,
)
from repo_agent.projects import ProjectContextResolver, ProjectRegistry
from repo_agent.react import ReActConfig, ReActExecutor, StructuredDecisionModel
from repo_agent.skills import (
    SkillAwareReActExecutor,
    SkillCatalog,
    SkillManager,
    register_skill_script_tools,
)
from repo_agent.tools import LocalRepositoryTools, build_repository_tool_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRANSIENT_ERRORS = (LLMRateLimitError, LLMTimeoutError, LLMTransportError)
CATEGORY_SYNONYMS = {
    "assertion_failure": ("assertion_failure", "断言失败", "断言错误", "逻辑错误", "运算符错误"),
    "collection_failure": ("collection_failure", "收集失败", "收集阶段失败", "收集阶段"),
    "environment_failure": ("environment_failure", "环境失败", "环境或依赖", "环境依赖", "环境配置错误"),
    "passed": ("passed", "通过"),
}


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
    """Count logical model calls, HTTP attempts, and latency without storing secrets."""

    def __init__(self, delegate: StructuredJSONClient, *, max_attempts: int = 3) -> None:
        self.delegate = delegate
        self.max_attempts = max_attempts
        self.logical_calls = 0
        self.http_attempts = 0
        self.latencies_ms: list[float] = []
        self._last_attempt_started = 0.0

    def generate_json(self, request: StructuredJSONRequest) -> Mapping[str, Any]:
        self.logical_calls += 1
        for attempt in range(1, self.max_attempts + 1):
            since_last_attempt = time.perf_counter() - self._last_attempt_started
            if since_last_attempt < 0.75:
                time.sleep(0.75 - since_last_attempt)
            self.http_attempts += 1
            started = time.perf_counter()
            self._last_attempt_started = started
            try:
                result = self.delegate.generate_json(request)
            except TRANSIENT_ERRORS:
                self.latencies_ms.append((time.perf_counter() - started) * 1000)
                if attempt == self.max_attempts:
                    raise
                time.sleep(2.0 * attempt)
            else:
                self.latencies_ms.append((time.perf_counter() - started) * 1000)
                return result
        raise AssertionError("retry loop ended unexpectedly")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [case["case_id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id must be unique")
    return cases


def contains_any(text: str, terms: list[str]) -> bool:
    normalized = text.casefold().replace(" ", "")
    return any(term.casefold().replace(" ", "") in normalized for term in terms)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provenance_comparison(
    expected: Mapping[str, Any],
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    field_matches = {
        field: supplied.get(field) == value
        for field, value in expected.items()
    }
    stdout_similarity = difflib.SequenceMatcher(
        None,
        str(expected["stdout"]),
        str(supplied.get("stdout", "")),
    ).ratio()
    stderr_similarity = difflib.SequenceMatcher(
        None,
        str(expected["stderr"]),
        str(supplied.get("stderr", "")),
    ).ratio()
    exact = all(field_matches.values())
    equivalent = (
        field_matches["exit_code"]
        and field_matches["timed_out"]
        and stdout_similarity >= 0.99
        and stderr_similarity >= 0.99
    )
    return {
        "exact": exact,
        "equivalent": equivalent,
        "field_matches": field_matches,
        "stdout_similarity": stdout_similarity,
        "stderr_similarity": stderr_similarity,
        "pytest_stdout_sha256": text_hash(str(expected["stdout"])),
        "classifier_stdout_sha256": text_hash(str(supplied.get("stdout", ""))),
        "pytest_stderr_sha256": text_hash(str(expected["stderr"])),
        "classifier_stderr_sha256": text_hash(str(supplied.get("stderr", ""))),
    }


def expected_category_terms(case: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            [
                *case["category_terms"],
                *CATEGORY_SYNONYMS.get(str(case["expected_category"]), ()),
            ]
        )
    )


def evaluate(case: Mapping[str, Any], result: Any, *, enabled: bool) -> dict[str, Any]:
    react = result.react_result
    answer = react.final_answer or ""
    events = react.events
    successful_reads = {
        str(event.arguments.get("path", "")).replace("\\", "/")
        for event in events
        if event.tool_name == "read_file_range" and event.result.ok
    }
    pytest_events = [event for event in events if event.tool_name == "run_pytest" and event.result.ok]
    expected_exit_code = int(case["expected_exit_code"])
    pytest_verified = any(
        event.result.metadata.get("test_exit_code") == expected_exit_code
        for event in pytest_events
    )
    path_evidence = all(path in successful_reads for path in case["required_paths"])
    category_correct = contains_any(answer, list(expected_category_terms(case)))
    facts_correct = all(contains_any(answer, list(group)) for group in case["required_fact_groups"])
    classifier_events = [
        event
        for event in events
        if event.tool_name == "classify_pytest_failure" and event.result.ok
    ]
    classifier_provenance = False
    classifier_provenance_exact = False
    provenance_diagnostics: list[dict[str, Any]] = []
    for classifier_event in classifier_events:
        classifier_index = events.index(classifier_event)
        for pytest_event in events[:classifier_index]:
            if pytest_event.tool_name != "run_pytest" or pytest_event.result.data is None:
                continue
            observation = pytest_event.result.data
            expected_arguments = {
                "stdout": observation.stdout,
                "stderr": observation.stderr,
                "exit_code": observation.exit_code,
                "timed_out": observation.timed_out,
            }
            comparison = provenance_comparison(
                expected_arguments,
                classifier_event.arguments,
            )
            provenance_diagnostics.append(comparison)
            if comparison["exact"]:
                classifier_provenance_exact = True
            if comparison["equivalent"]:
                classifier_provenance = True
                break
        if classifier_provenance:
            break
    selected_skill = result.active_skill.descriptor.name if result.active_skill else None
    completed = react.status == "completed" and bool(answer)
    return {
        "task_success": completed and pytest_verified and category_correct and facts_correct,
        "completed": completed,
        "pytest_verified": pytest_verified,
        "path_evidence": path_evidence,
        "category_correct": category_correct,
        "facts_correct": facts_correct,
        "classifier_used": bool(classifier_events),
        "classifier_provenance": classifier_provenance,
        "classifier_provenance_exact": classifier_provenance_exact,
        "classifier_provenance_diagnostics": provenance_diagnostics,
        "procedure_adherent": pytest_verified and path_evidence and classifier_provenance,
        "skill_selected": selected_skill == "diagnose-pytest-failure" if enabled else selected_skill is None,
    }


def evaluate_record(case: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute scores from persisted evidence without calling the model."""

    answer = str(record.get("final_answer") or "")
    trace = list(record.get("tool_trace", []))
    successful_reads = {
        str(event.get("arguments", {}).get("path", "")).replace("\\", "/")
        for event in trace
        if event.get("tool_name") == "read_file_range" and event.get("ok")
    }
    expected_exit_code = int(case["expected_exit_code"])
    pytest_verified = any(
        event.get("tool_name") == "run_pytest"
        and event.get("ok")
        and event.get("metadata", {}).get("test_exit_code") == expected_exit_code
        for event in trace
    )
    path_evidence = all(path in successful_reads for path in case["required_paths"])
    category_correct = contains_any(answer, list(expected_category_terms(case)))
    facts_correct = all(
        contains_any(answer, list(group)) for group in case["required_fact_groups"]
    )
    classifier_used = False
    classifier_provenance = False
    classifier_provenance_exact = False
    provenance_diagnostics: list[dict[str, Any]] = []
    for classifier_index, classifier_event in enumerate(trace):
        if (
            classifier_event.get("tool_name") != "classify_pytest_failure"
            or not classifier_event.get("ok")
        ):
            continue
        classifier_used = True
        for pytest_event in trace[:classifier_index]:
            if pytest_event.get("tool_name") != "run_pytest" or not pytest_event.get("data"):
                continue
            observation = pytest_event["data"]
            expected_arguments = {
                "stdout": observation["stdout"],
                "stderr": observation["stderr"],
                "exit_code": observation["exit_code"],
                "timed_out": observation["timed_out"],
            }
            comparison = provenance_comparison(
                expected_arguments,
                classifier_event.get("arguments", {}),
            )
            provenance_diagnostics.append(comparison)
            classifier_provenance_exact = (
                classifier_provenance_exact or comparison["exact"]
            )
            classifier_provenance = (
                classifier_provenance or comparison["equivalent"]
            )
    enabled = record.get("arm") == "enabled"
    selected_skill = record.get("active_skill")
    completed = record.get("status") == "completed" and bool(answer)
    return {
        "task_success": completed and pytest_verified and category_correct and facts_correct,
        "completed": completed,
        "pytest_verified": pytest_verified,
        "path_evidence": path_evidence,
        "category_correct": category_correct,
        "facts_correct": facts_correct,
        "classifier_used": classifier_used,
        "classifier_provenance": classifier_provenance,
        "classifier_provenance_exact": classifier_provenance_exact,
        "classifier_provenance_diagnostics": provenance_diagnostics,
        "procedure_adherent": pytest_verified and path_evidence and classifier_provenance,
        "skill_selected": (
            selected_skill == "diagnose-pytest-failure" if enabled else selected_skill is None
        ),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def aggregate(records: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    selected = [record for record in records if record["arm"] == arm]
    count = len(selected)
    metrics = ("task_success", "completed", "pytest_verified", "path_evidence", "category_correct", "facts_correct", "classifier_used", "classifier_provenance", "classifier_provenance_exact", "procedure_adherent", "skill_selected")
    result: dict[str, Any] = {"runs": count}
    for metric in metrics:
        passed = sum(bool(record["evaluation"][metric]) for record in selected)
        result[metric] = {"passed": passed, "rate": passed / count if count else 0.0}
    result["mean_tool_calls"] = statistics.fmean(record["tool_calls"] for record in selected) if selected else None
    result["mean_model_calls"] = statistics.fmean(record["model_logical_calls"] for record in selected) if selected else None
    result["mean_duration_ms"] = statistics.fmean(record["duration_ms"] for record in selected) if selected else None
    result["p95_duration_ms"] = percentile([record["duration_ms"] for record in selected], 0.95)
    return result


def write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "evals/benchmarks/skill-llm-ab-cases.jsonl")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output/benchmarks/skill-llm-ab.json")
    parser.add_argument("--repetitions", type=int, default=int(os.environ.get("SKILL_AB_REPETITIONS", "1")))
    parser.add_argument("--seed", type=int, default=20260809)
    parser.add_argument(
        "--case-ids",
        default=os.environ.get("SKILL_AB_CASE_IDS", ""),
        help="comma-separated case ids for a resumable pilot subset",
    )
    parser.add_argument(
        "--inter-run-delay-seconds",
        type=float,
        default=float(os.environ.get("SKILL_AB_INTER_RUN_DELAY_SECONDS", "60")),
    )
    parser.add_argument("--rescore-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if args.inter_run_delay_seconds < 0:
        raise ValueError("inter-run delay must be non-negative")
    dataset_path = args.dataset.resolve()
    output_path = args.output.resolve()
    cases = load_cases(dataset_path)
    requested_case_ids = tuple(
        item.strip() for item in args.case_ids.split(",") if item.strip()
    )
    if requested_case_ids:
        by_id = {case["case_id"]: case for case in cases}
        unknown = sorted(set(requested_case_ids) - set(by_id))
        if unknown:
            raise ValueError("unknown case ids: " + ", ".join(unknown))
        cases = [by_id[case_id] for case_id in requested_case_ids]
    dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    model_name = os.environ.get("GLM_MODEL", "glm-4.7-flash")
    existing: dict[str, Any] = {}
    if output_path.exists():
        candidate = json.loads(output_path.read_text(encoding="utf-8"))
        if args.rescore_only and candidate.get("dataset_sha256") == dataset_hash:
            model_name = str(candidate.get("model"))
            existing = candidate
        elif candidate.get("dataset_sha256") == dataset_hash and candidate.get("model") == model_name:
            existing = candidate
    records: list[dict[str, Any]] = list(existing.get("records", []))
    completed_keys = {record["run_key"] for record in records}

    if args.rescore_only:
        if not records:
            raise ValueError("no compatible persisted records to rescore")
        all_cases = {
            case["case_id"]: case for case in load_cases(dataset_path)
        }
        for record in records:
            record["evaluation"] = evaluate_record(
                all_cases[record["case_id"]],
                record,
            )
        existing["records"] = records
        existing["summary"] = {
            arm_name: aggregate(records, arm_name)
            for arm_name in ("disabled", "enabled")
        }
        existing["rescored_at"] = utc_now()
        existing.setdefault("protocol", {})["scoring_version"] = 2
        existing["protocol"]["classifier_provenance"] = (
            "exit_code/timed_out exact and stdout/stderr similarity >= 0.99; "
            "byte-exact equality is reported separately"
        )
        write_report(output_path, existing)
        print(json.dumps(existing["summary"], ensure_ascii=False, indent=2))
        return 0

    state_root = output_path.parent / "state/skill-llm-ab"
    resolver = ProjectContextResolver(ProjectRegistry(state_root / "projects.json"))
    base_client = structured_client_from_env("glm")
    metered = MeteredClient(base_client)
    schedule = [(case, repetition, arm) for repetition in range(1, args.repetitions + 1) for case in cases for arm in ("disabled", "enabled")]
    random.Random(args.seed).shuffle(schedule)

    try:
        executed_in_process = 0
        for index, (case, repetition, arm) in enumerate(schedule, start=1):
            run_key = f"{dataset_hash[:12]}:{model_name}:{case['case_id']}:{repetition}:{arm}"
            if run_key in completed_keys:
                print(f"[{index}/{len(schedule)}] resume {case['case_id']} {arm}", flush=True)
                continue
            if executed_in_process and args.inter_run_delay_seconds:
                print(
                    f"cooldown {args.inter_run_delay_seconds:.0f}s before next arm",
                    flush=True,
                )
                time.sleep(args.inter_run_delay_seconds)
            repo_root = (PROJECT_ROOT / case["repo"]).resolve()
            context = resolver.resolve(repo=repo_root)
            local_tools = LocalRepositoryTools(context, allow_code_execution=True)
            registry = build_repository_tool_registry(local_tools)
            catalog = SkillCatalog((PROJECT_ROOT / "skills",))
            discovery = catalog.refresh()
            if discovery.diagnostics:
                raise RuntimeError(f"Skill discovery failed: {discovery.diagnostics}")
            register_skill_script_tools(catalog, registry, allow_explicit_execution=True)
            decision_model = StructuredDecisionModel(
                StructuredDecisionClient(metered),
                max_validation_attempts=2,
            )
            react = ReActExecutor(decision_model, registry, config=ReActConfig(max_iterations=10, max_tool_calls=8, max_consecutive_tool_errors=2, max_identical_tool_calls=2))
            executor = SkillAwareReActExecutor(react, SkillManager(catalog, registry))
            before_calls = metered.logical_calls
            before_attempts = metered.http_attempts
            started = time.perf_counter()
            base_tools = ("list_files", "search_code", "read_file_range", "inspect_python", "run_pytest")
            result = executor.run(
                case["goal"],
                mode="diagnose",
                system_instructions=(
                    "目标仓库是固定的可执行评测夹具。必须依据工具观察回答；"
                    "完成前必须实际读取失败测试文件和直接被测源码；"
                    "最终结论应简洁说明失败分类、根因、文件证据和验证边界。"
                ),
                allowed_tools=(base_tools + ("classify_pytest_failure",)) if arm == "enabled" else base_tools,
                auto_route=arm == "enabled",
                runtime_required_tools=("run_pytest",),
                runtime_required_tool_counts={"run_pytest": 1, "read_file_range": 2},
            )
            duration_ms = (time.perf_counter() - started) * 1000
            evaluation = evaluate(case, result, enabled=arm == "enabled")
            record = {
                "run_key": run_key,
                "case_id": case["case_id"],
                "repetition": repetition,
                "arm": arm,
                "active_skill": result.active_skill.descriptor.name if result.active_skill else None,
                "status": result.react_result.status,
                "stop_reason": result.react_result.stop_reason,
                "final_answer": result.react_result.final_answer,
                "tool_calls": result.react_result.tool_calls,
                "model_logical_calls": metered.logical_calls - before_calls,
                "http_attempts": metered.http_attempts - before_attempts,
                "duration_ms": duration_ms,
                "tool_trace": [
                    {
                        "tool_name": event.tool_name,
                        "arguments": jsonable(event.arguments),
                        "ok": event.result.ok,
                        "metadata": jsonable(event.result.metadata),
                        "data": jsonable(event.result.data),
                        "error": jsonable(event.result.error),
                    }
                    for event in result.react_result.events
                ],
                "evaluation": evaluation,
            }
            records.append(record)
            executed_in_process += 1
            completed_keys.add(run_key)
            payload = {
                "benchmark": "skill_llm_ab_v5",
                "generated_at": utc_now(),
                "dataset": str(dataset_path),
                "dataset_sha256": dataset_hash,
                "model": model_name,
                "seed": args.seed,
                "requested_repetitions": args.repetitions,
                "selected_case_ids": [case["case_id"] for case in cases],
                "inter_run_delay_seconds": args.inter_run_delay_seconds,
                "protocol": {
                    "version": 5,
                    "treatment": "same ReAct/model/budget plus routed Skill instructions and scoped deterministic classifier",
                    "control": "same ReAct/model/budget without Skill activation",
                    "task_success": "completed answer + expected pytest exit + correct category + all fact groups",
                    "evidence_grounding": "both required files read successfully; reported separately from answer correctness",
                    "procedure_adherence": "expected pytest exit + both required files read + classifier input exactly matches a prior run_pytest observation",
                    "execution_authorization": "dedicated local benchmark fixtures only",
                },
                "summary": {arm_name: aggregate(records, arm_name) for arm_name in ("disabled", "enabled")},
                "records": records,
            }
            write_report(output_path, payload)
            print(f"[{index}/{len(schedule)}] {case['case_id']} {arm}: success={evaluation['task_success']} procedure={evaluation['procedure_adherent']} calls={record['model_logical_calls']}", flush=True)
    finally:
        close = getattr(base_client, "close", None)
        if callable(close):
            close()

    print(json.dumps({arm: aggregate(records, arm) for arm in ("disabled", "enabled")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
