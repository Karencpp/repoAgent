"""不依赖模型自评的工作流评估器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .models import EvaluationResult
from .ports import EvaluationRequest


_LINE_CITATION_PATTERN = re.compile(r"^.+:\d+(?:-\d+)?$")


def _string_sequence(value: object) -> tuple[str, ...]:
    """只接受字符串序列，避免把任意元数据误当作引用。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _observation_evidence(result: Mapping[str, Any]) -> tuple[str, ...]:
    """从统一工具结果中提取可回查的路径、行号和显式引用。"""

    evidence: list[str] = []
    metadata = result.get("metadata")
    if isinstance(metadata, Mapping):
        evidence.extend(_string_sequence(metadata.get("citations")))

    data = result.get("data")
    if isinstance(data, Mapping):
        path = data.get("path")
        start_line = data.get("start_line") or data.get("line_number")
        end_line = data.get("end_line") or start_line
        if isinstance(path, str) and path.strip():
            if isinstance(start_line, int) and isinstance(end_line, int):
                evidence.append(f"{path}:{start_line}-{end_line}")
            else:
                evidence.append(path)
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        for item in data:
            if not isinstance(item, Mapping):
                continue
            path = item.get("path")
            line = item.get("line_number")
            if isinstance(path, str) and path.strip():
                evidence.append(f"{path}:{line}" if isinstance(line, int) else path)

    return tuple(dict.fromkeys(evidence))


class EvidenceBasedDiagnoseEvaluator:
    """根据步骤状态和真实工具观察验收只读解释任务。"""

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """拒绝用模型的“已完成”声明替代工具证据。"""

        issues: list[str] = []
        strong_evidence: list[str] = []
        supporting_evidence: list[str] = []
        if not request.step_results:
            issues.append("没有步骤执行结果")

        expected_step_ids = {step.id for step in request.plan.steps}
        completed_step_ids = {
            result.step_id
            for result in request.step_results
            if result.status == "completed"
        }
        missing_steps = sorted(expected_step_ids - completed_step_ids)
        if missing_steps:
            issues.append("尚未完成步骤：" + "、".join(missing_steps))

        successful_observations = 0
        for step in request.step_results:
            for observation in step.observations:
                if observation.result.get("status") != "success":
                    continue
                successful_observations += 1
                extracted = _observation_evidence(observation.result)
                if extracted:
                    for item in extracted:
                        target = (
                            strong_evidence
                            if _LINE_CITATION_PATTERN.fullmatch(item)
                            else supporting_evidence
                        )
                        target.append(item)
                else:
                    supporting_evidence.append(
                        f"tool:{step.step_id}:{observation.iteration}:{observation.tool_name}"
                    )

        if successful_observations == 0:
            issues.append("没有成功的只读工具观察，无法证明结论来自目标仓库")

        unique_evidence = tuple(
            dict.fromkeys((*strong_evidence, *supporting_evidence))
        )[:20]
        passed = not issues
        summary = (
            f"全部 {len(expected_step_ids)} 个步骤完成，并取得 "
            f"{successful_observations} 条成功工具观察"
            if passed
            else "；".join(issues)
        )
        return EvaluationResult(
            passed=passed,
            summary=summary,
            issues=tuple(issues),
            evidence=unique_evidence,
        )
