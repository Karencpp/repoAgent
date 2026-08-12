"""最终答案合成、断言建模和逐条引用校验。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from repo_agent.projects import ProjectContext
from repo_agent.tools.repository import LocalRepositoryTools
from repo_agent.tools.schemas import ReadFileRangeInput

from .models import RepoAgentRunResult, StepExecution


_CITATION_PATTERN = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+):"
    r"(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?"
)


class FinalAnswerModel(BaseModel):
    """最终答案模型的公共严格配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerCitation(FinalAnswerModel):
    """一条经过仓库读取工具复核的引用。"""

    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    snippet_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @property
    def label(self) -> str:
        """返回人工可读的稳定引用标签。"""

        return f"{self.path}:{self.start_line}-{self.end_line}"


class AnswerClaim(FinalAnswerModel):
    """最终答案中的一条断言及其支撑状态。"""

    text: str = Field(min_length=1, max_length=2_000)
    citations: tuple[AnswerCitation, ...] = ()
    supported: bool


class FinalAnswer(FinalAnswerModel):
    """对外展示的最终答案及审计边界。"""

    answer: str = Field(min_length=1, max_length=40_000)
    claims: tuple[AnswerClaim, ...]
    citations: tuple[AnswerCitation, ...]
    limitations: tuple[str, ...] = Field(default=(), max_length=20)


class FinalAnswerSynthesisError(RuntimeError):
    """最终答案不能在当前客观证据上安全生成。"""


class FinalAnswerSynthesizerPort(Protocol):
    """最终答案合成端口，只消费已通过评估的步骤结果和证据。"""

    def synthesize(
        self,
        context: ProjectContext,
        result: RepoAgentRunResult,
    ) -> FinalAnswer:
        """生成带断言、引用和边界说明的最终答案。"""


def extract_citation_labels(text: str) -> tuple[str, ...]:
    """从工具摘要或评估证据中提取 `path:start-end` 引用标签。"""

    labels: list[str] = []
    for match in _CITATION_PATTERN.finditer(text):
        path = match.group("path").replace("\\", "/")
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end < start:
            continue
        label = f"{path}:{start}-{end}"
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def _citation_from_label(
    repository_tools: LocalRepositoryTools,
    label: str,
) -> AnswerCitation | None:
    """通过 `read_file_range` 复核引用路径和行号。"""

    path, _, line_range = label.rpartition(":")
    start_text, _, end_text = line_range.partition("-")
    start_line = int(start_text)
    end_line = int(end_text or start_text)
    result = repository_tools.read_file_range(
        ReadFileRangeInput(
            path=path,
            start_line=start_line,
            end_line=end_line,
            max_chars=20_000,
        )
    )
    if not result.ok or result.data is None:
        return None
    if result.data.end_line < start_line:
        return None
    return AnswerCitation(
        path=result.data.path,
        start_line=start_line,
        end_line=min(end_line, result.data.end_line),
    )


@dataclass(frozen=True, slots=True)
class DeterministicFinalAnswerSynthesizer:
    """基于已验证状态生成可复核最终答案，不调用模型做二次裁决。"""

    repository_tools_factory: type[LocalRepositoryTools] = LocalRepositoryTools

    def synthesize(
        self,
        context: ProjectContext,
        result: RepoAgentRunResult,
    ) -> FinalAnswer:
        """只在 Evaluator 通过后合成最终答案，并逐条复核引用。"""

        if result.evaluation is None or not result.evaluation.passed:
            raise FinalAnswerSynthesisError("最终答案只能基于已通过 Evaluator 的结果生成")
        if result.project_id != context.project_id:
            raise FinalAnswerSynthesisError("最终答案上下文与工作流项目不一致")
        if result.repo_revision != context.revision:
            raise FinalAnswerSynthesisError("最终答案上下文与当前仓库版本不一致")

        repository_tools = self.repository_tools_factory(context)
        valid_by_label: dict[str, AnswerCitation] = {}
        invalid_labels: list[str] = []
        evidence_text = "\n".join(result.evaluation.evidence)
        for label in extract_citation_labels(evidence_text):
            citation = _citation_from_label(repository_tools, label)
            if citation is None:
                invalid_labels.append(label)
                continue
            valid_by_label[citation.label] = citation

        claims = tuple(self._claims_from_steps(result.step_results, valid_by_label))
        unsupported = tuple(claim.text for claim in claims if not claim.supported)
        limitations: list[str] = []
        if invalid_labels:
            limitations.append(
                "以下引用未通过当前 revision 的 read_file_range 复核："
                + "、".join(invalid_labels)
            )
        if unsupported:
            limitations.append(
                "以下重要断言缺少可复核引用，已标记 unsupported："
                + "；".join(unsupported)
            )
        answer_lines = [
            f"# RepoAgent 最终答案：{result.user_goal}",
            "",
            result.evaluation.summary,
            "",
            "## 结论",
        ]
        answer_lines.extend(f"- {claim.text}" for claim in claims)
        if valid_by_label:
            answer_lines.extend(["", "## 引用"])
            answer_lines.extend(
                f"- {citation.label}" for citation in valid_by_label.values()
            )
        if limitations:
            answer_lines.extend(["", "## 边界"])
            answer_lines.extend(f"- {item}" for item in limitations)
        return FinalAnswer(
            answer="\n".join(answer_lines),
            claims=claims,
            citations=tuple(valid_by_label.values()),
            limitations=tuple(limitations),
        )

    @staticmethod
    def _claims_from_steps(
        steps: tuple[StepExecution, ...],
        valid_by_label: dict[str, AnswerCitation],
    ) -> list[AnswerClaim]:
        """把已完成步骤摘要转换成可审计断言。"""

        claims: list[AnswerClaim] = []
        for step in steps:
            if step.status != "completed":
                continue
            labels = extract_citation_labels(step.summary)
            citations = tuple(
                valid_by_label[label] for label in labels if label in valid_by_label
            )
            claims.append(
                AnswerClaim(
                    text=f"{step.step_id}：{step.summary}",
                    citations=citations,
                    supported=bool(citations),
                )
            )
        return claims
