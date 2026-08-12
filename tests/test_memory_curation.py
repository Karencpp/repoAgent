from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".memory-curation-test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.memory import (
    MemoryAwareWorkflowRunner,
    MemoryCandidate,
    MemoryCurationConflictError,
    MemoryManager,
    PerceptualObservation,
    MemorySearchRequest,
    MemoryWrite,
    SQLiteMemoryStore,
)
from repo_agent.projects import ProjectContext, ProjectContextResolver, ProjectRegistry
from repo_agent.rag import FeatureHashEmbeddingClient
from repo_agent.workflow import RepoAgentRunResult, StepExecution, StepToolObservation


class ScriptedJSONClient:
    """返回固定结构化对象的语义记忆提取客户端。"""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[object] = []

    def generate_json(self, request: object) -> dict[str, object]:
        self.requests.append(request)
        return self.response


def run_result(context: ProjectContext, run_id: str = "run-1") -> RepoAgentRunResult:
    """构造可被 Memory 自动接纳的客观工作流结果。"""

    return RepoAgentRunResult(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        project_id=context.project_id,
        repo_root=str(context.repo_root),
        repo_revision=context.revision,
        user_goal="修复支付重试错误",
        mode="fix",
        status="completed",
        plan=None,
        plan_history=(),
        step_results=(),
        evaluation=None,
        evaluation_history=(),
        reflection_history=(),
        reflection_count=0,
        replan_count=0,
        final_report="任务完成",
        stop_reason="验收通过",
        trace=(),
    )


class FakeWorkflow:
    """只用于验证工作流完成后的自动记忆触发。"""

    def run(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        mode: str = "diagnose",
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> RepoAgentRunResult:
        del user_goal, mode, thread_id, checkpoint_thread_id
        return run_result(context, run_id or "fake-run")


class PerceptualWorkflow:
    """返回带结构化制品观察元数据的工作流。"""

    def run(
        self,
        context: ProjectContext,
        user_goal: str,
        *,
        mode: str = "diagnose",
        run_id: str | None = None,
        thread_id: str | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> RepoAgentRunResult:
        del user_goal, mode, thread_id, checkpoint_thread_id
        result = run_result(context, run_id or "perceptual-run")
        observation = StepToolObservation(
            iteration=1,
            tool_name="inspect_ci_artifact",
            arguments={"artifact_uri": "artifact://ci/run-88/screenshot-1"},
            decision_summary="读取 CI 失败截图",
            result={
                "status": "success",
                "data": {"width": 1280, "height": 720},
                "error": None,
                "metadata": {
                    "perceptual_observations": [
                        {
                            "observation_id": "ci-run-88-screenshot-1",
                            "artifact_uri": "artifact://ci/run-88/screenshot-1",
                            "media_type": "image/png",
                            "description": "CI 截图显示退款测试在 Windows 环境超时",
                            "observed_by": "tool",
                            "claim_status": "verified",
                            "scope": "revision",
                            "importance": 0.7,
                            "evidence": ["ci:run-88"],
                            "tags": ["ci", "timeout"],
                        }
                    ]
                },
            },
        )
        step = StepExecution(
            step_id="inspect-ci",
            status="completed",
            summary="已经读取 CI 制品",
            react_status="completed",
            stop_reason="制品读取完成",
            iterations=1,
            tool_calls=1,
            observations=(observation,),
        )
        return result.model_copy(update={"step_results": (step,)})


class MemoryCurationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir(parents=True)
        (self.repo / "service.py").write_text("RETRY = 3\n", encoding="utf-8")
        self.context = ProjectContextResolver(
            ProjectRegistry(self.state / "projects.json")
        ).resolve(repo=self.repo)
        self.store = SQLiteMemoryStore(
            self.state / "memory.sqlite3",
            FeatureHashEmbeddingClient(128),
        )
        self.manager = MemoryManager(self.store)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_same_workflow_run_is_idempotent(self) -> None:
        result = run_result(self.context)

        first = self.manager.record_run(self.context, result)
        second = self.manager.record_run(self.context, result)
        search = self.store.search(
            self.context,
            MemorySearchRequest(query="支付重试", top_k=10),
        )

        self.assertEqual(first.memory_id, second.memory_id)
        self.assertEqual(len(search.hits), 1)

    def test_model_verified_fact_waits_for_review_then_is_created(self) -> None:
        candidate = MemoryCandidate(
            candidate_id="candidate:model-retry",
            memory_key="config:retry-limit",
            proposed_by="model",
            rationale="模型从代码片段中推断出重试次数",
            memory=MemoryWrite(
                memory_type="semantic",
                content="支付重试上限为三次",
                claim_status="verified",
                scope="revision",
                repo_revision=self.context.revision,
                source="tool",
                source_id="read-file-1",
                evidence=("service.py:1",),
            ),
        )

        pending = self.manager.submit_candidate(self.context, candidate)
        approved = self.manager.review_candidate(
            self.context,
            candidate.candidate_id,
            approve=True,
            reviewer="user:interviewer",
            reason="已核对目标仓库当前版本",
        )

        self.assertEqual(pending.action, "pending_review")
        self.assertEqual(len(self.manager.pending_reviews(self.context)), 0)
        self.assertEqual(approved.action, "created")
        self.assertIsNotNone(approved.result_memory_id)

    def test_verified_change_waits_for_review_and_supersedes_old_fact(self) -> None:
        old = self.manager.remember_verified_fact(
            self.context,
            "支付重试上限为三次",
            evidence=("service.py:1",),
            source_id="config-v1",
            memory_key="config:retry-limit",
        )
        candidate = MemoryCandidate(
            candidate_id="candidate:retry-v2",
            memory_key="config:retry-limit",
            proposed_by="user",
            rationale="用户提交配置变更后的新事实",
            memory=MemoryWrite(
                memory_type="semantic",
                content="支付重试上限为五次",
                claim_status="verified",
                scope="revision",
                repo_revision=self.context.revision,
                source="user",
                source_id="config-v2",
                evidence=("service.py:1",),
            ),
        )

        pending = self.manager.submit_candidate(self.context, candidate)
        approved = self.manager.review_candidate(
            self.context,
            candidate.candidate_id,
            approve=True,
            reviewer="user:owner",
            reason="确认新配置已经生效",
        )

        self.assertEqual(pending.action, "pending_review")
        self.assertEqual(approved.action, "superseded")
        self.assertEqual(self.store.get(self.context, old.memory_id).status, "superseded")
        self.assertEqual(
            self.store.get(self.context, approved.result_memory_id).content,
            "支付重试上限为五次",
        )

    def test_verified_evidence_can_promote_hypothesis_automatically(self) -> None:
        hypothesis = self.manager.remember_hypothesis(
            self.context,
            "缓存失效可能导致旧配置",
            source_id="investigation-1",
            memory_key="cause:stale-config",
        )

        verified = self.manager.remember_verified_fact(
            self.context,
            "缓存失效会导致旧配置",
            evidence=("test_cache.py::test_refresh:passed",),
            source_id="evaluation-1",
            memory_key="cause:stale-config",
        )

        self.assertEqual(self.store.get(self.context, hypothesis.memory_id).status, "superseded")
        self.assertEqual(verified.claim_status, "verified")
        self.assertEqual(verified.supersedes_memory_id, hypothesis.memory_id)

    def test_review_detects_fact_changed_while_waiting(self) -> None:
        self.manager.remember_verified_fact(
            self.context,
            "重试上限为三次",
            evidence=("service.py:1",),
            source_id="v1",
            memory_key="config:retry",
        )
        waiting = MemoryCandidate(
            candidate_id="candidate:waiting-v2",
            memory_key="config:retry",
            proposed_by="user",
            rationale="等待审核的新配置",
            memory=MemoryWrite(
                memory_type="semantic",
                content="重试上限为五次",
                claim_status="verified",
                scope="revision",
                repo_revision=self.context.revision,
                source="user",
                source_id="v2",
                evidence=("service.py:1",),
            ),
        )
        self.manager.submit_candidate(self.context, waiting)
        active = self.store.find_active_by_key(self.context, "config:retry")
        self.store.supersede(
            self.context,
            active.memory_id,
            MemoryWrite(
                memory_type="semantic",
                content="重试上限为四次",
                claim_status="verified",
                scope="revision",
                repo_revision=self.context.revision,
                source="manual",
                source_id="v-between",
                evidence=("service.py:1",),
            ),
        )

        with self.assertRaises(MemoryCurationConflictError):
            self.manager.review_candidate(
                self.context,
                waiting.candidate_id,
                approve=True,
                reviewer="user:owner",
                reason="准备批准",
            )

    def test_expired_memory_is_not_recalled_before_physical_cleanup(self) -> None:
        expired = self.store.put(
            self.context,
            MemoryWrite(
                memory_type="episodic",
                content="已经失效的临时部署结论",
                claim_status="verified",
                scope="project",
                source="workflow",
                source_id="old-run",
                evidence=("run:old-run",),
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )

        result = self.store.search(
            self.context,
            MemorySearchRequest(query="临时部署"),
        )

        self.assertEqual(result.hits, ())
        self.assertEqual(self.store.get(self.context, expired.memory_id).status, "active")

    def test_forget_erases_content_and_records_actor_and_reason(self) -> None:
        memory = self.manager.remember_verified_fact(
            self.context,
            "不应继续保留的用户偏好",
            evidence=("user:message-1",),
            source_id="preference-1",
            scope="project",
        )

        self.manager.forget(
            self.context,
            memory.memory_id,
            requested_by="user:owner",
            reason="用户撤回这条偏好",
        )
        tombstone = self.store.get(self.context, memory.memory_id)
        events = self.store.list_lifecycle_events(
            self.context,
            memory_id=memory.memory_id,
        )

        self.assertEqual(tombstone.status, "forgotten")
        self.assertEqual(tombstone.content, "[已遗忘]")
        self.assertEqual(events[0].actor, "user:owner")
        self.assertEqual(events[0].reason, "用户撤回这条偏好")

    def test_workflow_wrapper_triggers_memory_automatically(self) -> None:
        runner = MemoryAwareWorkflowRunner(FakeWorkflow(), self.manager)

        result = runner.run(
            self.context,
            "修复支付重试错误",
            mode="fix",
            run_id="auto-run",
        )

        self.assertEqual(result.workflow_result.run_id, "auto-run")
        self.assertEqual(result.memory_decision.action, "created")
        stored = self.store.get(
            self.context,
            result.memory_decision.result_memory_id,
        )
        self.assertIn("修复支付重试错误", stored.content)

    def test_workflow_automatically_extracts_semantic_candidates(self) -> None:
        client = ScriptedJSONClient(
            {
                "drafts": [
                    {
                        "memory_key": "architecture:payment-retry-storage",
                        "content": "支付重试状态可能持久化在数据库中",
                        "claim_status": "hypothesis",
                        "importance": 0.6,
                        "scope": "project",
                        "evidence": [],
                        "tags": ["payment", "retry"],
                        "rationale": "该结论可能帮助后续排查重复扣款",
                    },
                    {
                        "memory_key": "testing:payment-command",
                        "content": "支付模块存在可重复执行的验证流程",
                        "claim_status": "verified",
                        "importance": 0.8,
                        "scope": "revision",
                        "evidence": ["run:semantic-run"],
                        "tags": ["testing"],
                        "rationale": "本次运行提供了可追溯执行证据",
                    },
                ]
            }
        )
        runner = MemoryAwareWorkflowRunner(
            FakeWorkflow(),
            self.manager,
            semantic_client=client,
        )

        result = runner.run(
            self.context,
            "修复支付重试错误",
            mode="fix",
            run_id="semantic-run",
        )

        actions = [item.action for item in result.formation_decisions]
        self.assertEqual(actions, ["created", "pending_review"])
        self.assertEqual(result.formation_errors, ())
        hypotheses = self.store.search(
            self.context,
            MemorySearchRequest(
                query="支付重试状态",
                claim_statuses=("hypothesis",),
            ),
        )
        self.assertEqual(hypotheses.hits[0].record.memory_type, "semantic")
        self.assertEqual(len(self.manager.pending_reviews(self.context)), 1)
        self.assertEqual(len(client.requests), 1)

    def test_workflow_automatically_forms_perceptual_memory(self) -> None:
        runner = MemoryAwareWorkflowRunner(
            PerceptualWorkflow(),
            self.manager,
            trusted_perception_tools=("inspect_ci_artifact",),
        )

        result = runner.run(
            self.context,
            "分析 CI 失败截图",
            run_id="perceptual-run",
        )

        self.assertEqual(len(result.formation_decisions), 1)
        decision = result.formation_decisions[0]
        self.assertEqual(decision.action, "created")
        memory = self.store.get(self.context, decision.result_memory_id)
        self.assertEqual(memory.memory_type, "perceptual")
        self.assertEqual(memory.source, "tool")
        self.assertIn("artifact:artifact://ci/run-88/screenshot-1", memory.evidence)
        self.assertIsNotNone(memory.expires_at)

    def test_model_perceptual_verified_observation_requires_review(self) -> None:
        decision = self.manager.remember_perceptual_observation(
            self.context,
            PerceptualObservation(
                observation_id="vision-result-1",
                artifact_uri="artifact://local/failure.png",
                media_type="image/png",
                description="截图显示页面可能出现重复提交按钮",
                observed_by="model",
                claim_status="verified",
                evidence=("vision:request-1",),
            ),
        )

        self.assertEqual(decision.action, "pending_review")
        self.assertIsNone(decision.result_memory_id)

    def test_untrusted_tool_perceptual_claim_is_downgraded_to_hypothesis(self) -> None:
        runner = MemoryAwareWorkflowRunner(PerceptualWorkflow(), self.manager)

        result = runner.run(
            self.context,
            "分析未受信任工具返回的截图",
            run_id="untrusted-perceptual-run",
        )

        decision = result.formation_decisions[0]
        memory = self.store.get(self.context, decision.result_memory_id)
        self.assertEqual(memory.claim_status, "hypothesis")

    def test_semantic_extraction_failure_does_not_erase_workflow_result(self) -> None:
        client = ScriptedJSONClient(
            {
                "drafts": [
                    {
                        "memory_key": "architecture:invented",
                        "content": "没有运行证据支持的结论",
                        "claim_status": "verified",
                        "importance": 0.8,
                        "scope": "project",
                        "evidence": ["invented:evidence"],
                        "tags": [],
                        "rationale": "用于验证失败隔离",
                    }
                ]
            }
        )
        runner = MemoryAwareWorkflowRunner(
            FakeWorkflow(),
            self.manager,
            semantic_client=client,
        )

        result = runner.run(
            self.context,
            "修复支付重试错误",
            run_id="formation-error-run",
        )

        self.assertEqual(result.workflow_result.status, "completed")
        self.assertEqual(result.memory_decision.action, "created")
        self.assertEqual(result.formation_decisions, ())
        self.assertEqual(len(result.formation_errors), 1)

    def test_multiple_episodes_can_be_consolidated_into_semantic_candidate(self) -> None:
        first = self.manager.record_run(
            self.context,
            run_result(self.context, "payment-retry-1"),
        )
        second_result = run_result(self.context, "payment-retry-2").model_copy(
            update={"user_goal": "再次排查支付重试错误"}
        )
        second = self.manager.record_run(self.context, second_result)
        client = ScriptedJSONClient(
            {
                "drafts": [
                    {
                        "memory_key": "failure-pattern:payment-retry",
                        "content": "支付重试问题可能具有重复发生的故障模式",
                        "claim_status": "verified",
                        "importance": 0.8,
                        "scope": "project",
                        "evidence": [
                            f"memory:{first.memory_id}",
                            f"memory:{second.memory_id}",
                        ],
                        "tags": ["payment", "failure-pattern"],
                        "rationale": "两个独立任务都涉及相同故障主题",
                    }
                ]
            }
        )

        decisions = self.manager.consolidate_semantic_memories(
            self.context,
            "支付重试错误",
            client=client,
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "pending_review")
        self.assertEqual(
            decisions[0].candidate.memory.tags[-1],
            "semantic-consolidation",
        )


if __name__ == "__main__":
    unittest.main()
