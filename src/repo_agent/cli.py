"""RepoAgent 命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from repo_agent.application import RepoAgentApplication, RepoAgentApplicationConfig
from repo_agent.evals import evaluate_patch_cases, evaluate_retrieval_cases
from repo_agent.evals.explain_runner import evaluate_explain_cases
from repo_agent.evals.report import write_report
from repo_agent.maintenance import RepoAgentMaintenanceService
from repo_agent.memory import MemoryManager
from repo_agent.migration import migrate_state
from repo_agent.projects import ProjectRegistry
from repo_agent.rag import FeatureHashEmbeddingClient
from repo_agent.llm import structured_client_from_env
from repo_agent.storage import InfrastructureFactory


def _state_dir(value: str | None) -> Path | None:
    """把可选命令行目录转换为路径。"""

    return Path(value).expanduser() if value else None


def _application_config(arguments: argparse.Namespace) -> RepoAgentApplicationConfig:
    """从公共命令行选项构造应用配置。"""

    return RepoAgentApplicationConfig(
        state_dir=_state_dir(arguments.state_dir),
        skills_root=_state_dir(getattr(arguments, "skills_root", None)),
        mcp_config_path=_state_dir(getattr(arguments, "mcp_config", None)),
        enable_rag=not getattr(arguments, "no_rag", False),
        enable_memory=not getattr(arguments, "no_memory", False),
        form_semantic_memory=not getattr(
            arguments,
            "no_semantic_memory",
            False,
        ),
        storage_backend=getattr(arguments, "storage_backend", None),
        postgres_dsn=getattr(arguments, "postgres_dsn", None),
        embedding_provider=(
            "glm" if getattr(arguments, "use_glm_embedding", False) else "local"
        ),
        llm_provider=getattr(arguments, "llm_provider", None),
    )


def _add_project_selector(parser: argparse.ArgumentParser) -> None:
    """添加强制显式选择目标项目的互斥参数。"""

    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--repo", help="目标 Python 代码库的路径")
    selection.add_argument("--project", help="已注册项目的名称或 project_id")


def _add_storage_options(parser: argparse.ArgumentParser) -> None:
    """添加 SQLite/PostgreSQL 双后端公共参数。"""

    parser.add_argument(
        "--storage-backend",
        choices=("sqlite", "postgres"),
        help="持久化后端；默认读取环境变量或使用 sqlite",
    )
    parser.add_argument(
        "--postgres-dsn",
        help="PostgreSQL DSN；只在 --storage-backend postgres 时使用",
    )


def build_parser() -> argparse.ArgumentParser:
    """声明稳定且便于脚本调用的命令行协议。"""

    parser = argparse.ArgumentParser(
        prog="repo-agent",
        description="显式选择目标仓库的 Python 代码维护 Agent",
    )
    parser.add_argument(
        "--state-dir",
        help="RAG、Memory、Checkpoint 和项目注册表的独立状态目录",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    explain = subparsers.add_parser("explain", help="只读解释目标代码库")
    _add_project_selector(explain)
    explain.add_argument(
        "question",
        nargs="?",
        help="希望 Agent 回答的问题；省略后从控制台读取",
    )
    explain.add_argument("--thread-id", help="可恢复的逻辑线程标识")
    explain.add_argument("--skills-root", help="显式可信 Skill 根目录")
    explain.add_argument("--mcp-config", help="显式 MCP Server 配置 JSON 文件")
    _add_storage_options(explain)
    explain.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；未提供时读取 LLM_PROVIDER，默认 glm",
    )
    explain.add_argument("--no-rag", action="store_true", help="关闭代码库索引")
    explain.add_argument("--no-memory", action="store_true", help="关闭长期记忆")
    explain.add_argument(
        "--use-glm-embedding",
        action="store_true",
        help="使用远程 GLM Embedding，需要显式设置外发代码授权环境变量",
    )
    explain.add_argument(
        "--no-semantic-memory",
        action="store_true",
        help="不在任务结束后调用模型提取语义记忆",
    )
    explain.set_defaults(handler=_handle_explain)

    chat = subparsers.add_parser("chat", help="连续输入多个只读代码库问题")
    _add_project_selector(chat)
    chat.add_argument("--skills-root", help="显式可信 Skill 根目录")
    chat.add_argument("--mcp-config", help="显式 MCP Server 配置 JSON 文件")
    _add_storage_options(chat)
    chat.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；未提供时读取 LLM_PROVIDER，默认 glm",
    )
    chat.add_argument("--no-rag", action="store_true", help="关闭代码库索引")
    chat.add_argument("--no-memory", action="store_true", help="关闭长期记忆")
    chat.add_argument(
        "--use-glm-embedding",
        action="store_true",
        help="使用远程 GLM Embedding，需要显式设置外发代码授权环境变量",
    )
    chat.add_argument(
        "--no-semantic-memory",
        action="store_true",
        help="不在每次回答后调用模型提取语义记忆",
    )
    chat.set_defaults(handler=_handle_chat)

    resume = subparsers.add_parser("resume", help="恢复同一项目的 checkpoint 线程")
    _add_project_selector(resume)
    resume.add_argument("--thread-id", required=True, help="需要恢复的逻辑线程标识")
    resume.add_argument("--skills-root", help="显式可信 Skill 根目录")
    resume.add_argument("--mcp-config", help="显式 MCP Server 配置 JSON 文件")
    _add_storage_options(resume)
    resume.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；必须与原线程使用兼容配置",
    )
    resume.add_argument("--no-rag", action="store_true", help="关闭代码库索引")
    resume.add_argument("--no-memory", action="store_true", help="关闭长期记忆")
    resume.add_argument(
        "--use-glm-embedding",
        action="store_true",
        help="使用远程 GLM Embedding，需要显式设置外发代码授权环境变量",
    )
    resume.add_argument(
        "--no-semantic-memory",
        action="store_true",
        help="恢复完成后不调用模型提取语义记忆",
    )
    resume.set_defaults(handler=_handle_resume)

    fix = subparsers.add_parser("fix", help="生成并验证隔离候选修改")
    _add_project_selector(fix)
    fix.add_argument("objective", help="希望 Agent 完成的维护目标")
    fix.add_argument("--thread-id", help="只读分析阶段的逻辑线程标识")
    fix.add_argument("--skills-root", help="显式可信 Skill 根目录")
    fix.add_argument("--mcp-config", help="显式 MCP Server 配置 JSON 文件")
    _add_storage_options(fix)
    fix.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；未提供时读取 LLM_PROVIDER，默认 glm",
    )
    fix.add_argument("--no-rag", action="store_true", help="关闭代码库索引")
    fix.add_argument("--no-memory", action="store_true", help="关闭长期记忆")
    fix.add_argument(
        "--use-glm-embedding",
        action="store_true",
        help="使用远程 GLM Embedding，需要显式设置外发代码授权环境变量",
    )
    fix.add_argument(
        "--no-semantic-memory",
        action="store_true",
        help="不在分析结束后调用模型提取语义记忆",
    )
    fix.add_argument(
        "--allow-code-execution",
        action="store_true",
        help="明确授权在隔离候选副本中运行 pytest",
    )
    fix.set_defaults(handler=_handle_fix)

    resume_fix = subparsers.add_parser("resume-fix", help="恢复维护工作流审批")
    _add_project_selector(resume_fix)
    resume_fix.add_argument("--thread-id", required=True, help="维护工作流线程标识")
    decision = resume_fix.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true", help="批准并回写")
    decision.add_argument("--reject", action="store_true", help="拒绝候选修改")
    resume_fix.add_argument("--skills-root", help="显式可信 Skill 根目录")
    resume_fix.add_argument("--mcp-config", help="显式 MCP Server 配置 JSON 文件")
    _add_storage_options(resume_fix)
    resume_fix.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；必须与原线程兼容",
    )
    resume_fix.add_argument("--no-rag", action="store_true", help="关闭代码库索引")
    resume_fix.add_argument("--no-memory", action="store_true", help="关闭长期记忆")
    resume_fix.add_argument(
        "--use-glm-embedding",
        action="store_true",
        help="使用远程 GLM Embedding",
    )
    resume_fix.add_argument(
        "--no-semantic-memory",
        action="store_true",
        help="不形成语义记忆",
    )
    resume_fix.set_defaults(handler=_handle_resume_fix)

    apply_command = subparsers.add_parser("apply", help="批准并回写已验证候选")
    apply_command.add_argument("--proposal-id", required=True, help="候选制品标识")
    apply_command.add_argument(
        "--approve",
        action="store_true",
        help="明确批准把已验证候选回写到真实仓库",
    )
    apply_command.set_defaults(handler=_handle_apply)

    eval_command = subparsers.add_parser("eval", help="运行离线评测数据集")
    eval_subparsers = eval_command.add_subparsers(dest="eval_command", required=True)
    eval_retrieval = eval_subparsers.add_parser("retrieval", help="评测 RAG 检索")
    eval_retrieval.add_argument("--dataset", required=True, help="JSONL 检索数据集")
    eval_retrieval.add_argument("--fixtures-root", default="evals/fixtures")
    eval_retrieval.add_argument("--output", help="可选 JSON 报告输出路径")
    eval_retrieval.add_argument(
        "--mode",
        choices=("lexical", "dense", "hybrid"),
        default="hybrid",
    )
    eval_retrieval.set_defaults(handler=_handle_eval_retrieval)

    eval_explain = eval_subparsers.add_parser("explain", help="评测解释数据集")
    eval_explain.add_argument("--dataset", required=True, help="JSONL 解释数据集")
    eval_explain.add_argument("--fixtures-root", default="evals/fixtures")
    eval_explain.add_argument("--output", help="可选 JSON 报告输出路径")
    eval_explain.set_defaults(handler=_handle_eval_explain)

    eval_patch = eval_subparsers.add_parser("patch", help="评测 Patch 修复数据集")
    eval_patch.add_argument("--dataset", required=True, help="JSONL Patch 数据集")
    eval_patch.add_argument("--fixtures-root", default="evals/fixtures")
    eval_patch.add_argument("--output", help="可选 JSON 报告输出路径")
    eval_patch.set_defaults(handler=_handle_eval_patch)

    migrate = subparsers.add_parser(
        "migrate-state",
        help="把 SQLite 状态迁移到 PostgreSQL",
    )
    migrate.add_argument("--sqlite-state-dir", required=True, help="SQLite 状态目录")
    migrate.add_argument("--postgres-dsn", required=True, help="目标 PostgreSQL DSN")
    migrate_mode = migrate.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument("--dry-run", action="store_true", help="只报告可迁移计数")
    migrate_mode.add_argument("--execute", action="store_true", help="执行事务化迁移")
    migrate.set_defaults(handler=_handle_migrate_state)

    project = subparsers.add_parser("project", help="管理目标项目注册表")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    add = project_subparsers.add_parser("add", help="注册一个目标代码库")
    add.add_argument("--repo", required=True, help="目标代码库路径")
    add.add_argument("--name", required=True, help="稳定项目名称")
    add.set_defaults(handler=_handle_project_add)
    listing = project_subparsers.add_parser("list", help="列出已注册项目")
    listing.set_defaults(handler=_handle_project_list)

    memory = subparsers.add_parser("memory", help="管理长期记忆慢路径")
    memory_subparsers = memory.add_subparsers(dest="memory_command", required=True)
    consolidate = memory_subparsers.add_parser(
        "consolidate",
        help="把多条已验证情景记忆归纳为语义候选",
    )
    _add_project_selector(consolidate)
    consolidate.add_argument("--topic", required=True, help="归纳主题")
    consolidate.add_argument("--top-k", type=int, default=10, help="检索情景记忆数量")
    _add_storage_options(consolidate)
    consolidate.add_argument(
        "--llm-provider",
        choices=("glm", "deepseek"),
        help="推理模型供应商；未提供时读取 LLM_PROVIDER，默认 glm",
    )
    consolidate.set_defaults(handler=_handle_memory_consolidate)
    return parser


def _registry(arguments: argparse.Namespace) -> ProjectRegistry:
    """使用与应用运行相同的注册表位置。"""

    state_dir = _application_config(arguments).resolved_state_dir()
    return ProjectRegistry(state_dir / "projects.json")


def _handle_project_add(arguments: argparse.Namespace) -> int:
    """注册项目并输出可复制的稳定身份。"""

    registration = _registry(arguments).register(arguments.repo, arguments.name)
    print(json.dumps(registration.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _handle_project_list(arguments: argparse.Namespace) -> int:
    """以 JSON 输出项目列表，避免表格截断路径。"""

    projects = [item.to_dict() for item in _registry(arguments).list()]
    print(json.dumps(projects, ensure_ascii=False, indent=2))
    return 0


def _handle_memory_consolidate(arguments: argparse.Namespace) -> int:
    """显式运行语义记忆慢路径归纳。"""

    config = _application_config(arguments)
    application = RepoAgentApplication(config)
    context = application.resolve_project(
        repo=arguments.repo,
        project=arguments.project,
    )
    embedding = FeatureHashEmbeddingClient(config.rag_embedding_dimensions)
    client = structured_client_from_env(arguments.llm_provider)
    close_client = getattr(client, "close", None)
    try:
        store = InfrastructureFactory(
            config.storage_config(),
            embedding_client=embedding,
        ).create_memory_store()
        try:
            decisions = MemoryManager(store).consolidate_semantic_memories(
                context,
                arguments.topic,
                client=client,
                top_k=arguments.top_k,
            )
        finally:
            store.close()
    finally:
        if callable(close_client):
            close_client()
    print(
        json.dumps(
            {
                "project_id": context.project_id,
                "repo_revision": context.revision,
                "topic": arguments.topic,
                "decisions": [
                    decision.model_dump(mode="json") for decision in decisions
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _handle_explain(arguments: argparse.Namespace) -> int:
    """执行真实只读解释链路并展示外围模块摘要。"""

    question = arguments.question
    if question is None:
        question = input("请输入代码库问题：").strip()
    if not question:
        raise ValueError("代码库问题不能为空")
    application = RepoAgentApplication(_application_config(arguments))
    result = application.explain(
        question,
        repo=arguments.repo,
        project=arguments.project,
        thread_id=arguments.thread_id,
        progress_callback=_console_progress,
    )
    print(result.workflow.final_report)
    print("\n## 运行时装配")
    print(f"- 目标仓库：{result.context.repo_root}")
    print(f"- 项目标识：{result.context.project_id}")
    print(f"- 线程标识：{result.workflow.thread_id}")
    if result.indexing is not None:
        print(
            "- RAG 索引："
            f"扫描 {result.indexing.scanned_files} 个文件，"
            f"更新 {result.indexing.indexed_files} 个文件，"
            f"写入 {result.indexing.written_chunks} 个分块"
        )
    print(
        "- 已发现 Skill："
        + ("、".join(result.discovered_skills) or "无")
    )
    print(f"- Memory 治理决策：{len(result.memory_decisions)} 条")
    print(f"- Context 构建：{len(result.context_builds)} 次")
    print(
        "- Context 压缩："
        f"{sum(len(item.compressions) for item in result.context_builds)} 个 Packet"
    )
    for error in result.memory_errors:
        print(f"- Memory 附属阶段警告：{error}")
    return 0 if result.workflow.status == "completed" else 2


def _handle_chat(arguments: argparse.Namespace) -> int:
    """在同一目标项目和状态目录上连续执行只读解释任务。"""

    application = RepoAgentApplication(_application_config(arguments))
    print("RepoAgent 交互模式已启动。输入代码库问题；输入 退出、exit 或 quit 结束。")
    while True:
        try:
            question = input("\nRepoAgent> ").strip()
        except EOFError:
            print("\n输入流已关闭，结束交互模式。")
            return 0
        if question.casefold() in {"退出", "exit", "quit"}:
            print("已结束 RepoAgent 交互模式。")
            return 0
        if not question:
            continue
        try:
            result = application.explain(
                question,
                repo=arguments.repo,
                project=arguments.project,
                progress_callback=_console_progress,
            )
        except Exception as exc:
            print(f"本次问题执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print("\n" + result.workflow.final_report)
        if result.memory_errors:
            for error in result.memory_errors:
                print(f"Memory 附属阶段警告：{error}")


def _console_progress(message: str) -> None:
    """立即刷新一条适合 PyCharm Run Console 的进度事件。"""

    print(f"[进度] {message}", flush=True)


def _handle_resume(arguments: argparse.Namespace) -> int:
    """恢复经过项目身份和 revision 校验的工作流线程。"""

    application = RepoAgentApplication(_application_config(arguments))
    result = application.explain(
        "",
        repo=arguments.repo,
        project=arguments.project,
        thread_id=arguments.thread_id,
        resume=True,
    )
    print(result.workflow.final_report)
    return 0 if result.workflow.status == "completed" else 2


def _handle_fix(arguments: argparse.Namespace) -> int:
    """运行维护工作流直到等待人工审批。"""

    service = RepoAgentMaintenanceService(_application_config(arguments))
    result = service.start_workflow(
        arguments.objective,
        repo=arguments.repo,
        project=arguments.project,
        allow_code_execution=arguments.allow_code_execution,
        thread_id=arguments.thread_id,
    )
    print(f"# RepoAgent 维护工作流：{result.objective}")
    print(f"\n- 线程标识：{result.thread_id}")
    print(f"- 候选标识：{result.proposal_id or '未生成'}")
    print(f"- 目标仓库：{result.repo_root}")
    print(f"- 工作流状态：{result.status}")
    print(f"- Patch 尝试次数：{result.patch_attempt}")
    print("\n## 修改差异")
    print((result.evaluation.unified_diff if result.evaluation else "") or "没有差异")
    print("\n## 客观验证")
    for check in (result.evaluation.checks if result.evaluation else ()):
        print(f"- {check.name} [{check.status}]：{check.summary}")
    if result.status == "waiting_approval" and result.proposal_id:
        print(
            "\n真实仓库尚未修改。确认差异后执行："
            f"repo-agent resume-fix --thread-id {result.thread_id} "
            f"--repo {result.repo_root} --approve"
        )
        print(
            "兼容入口仍可使用："
            f"repo-agent apply --proposal-id {result.proposal_id} --approve"
        )
        return 0
    return 2


def _handle_resume_fix(arguments: argparse.Namespace) -> int:
    """用明确审批结果恢复维护工作流。"""

    result = RepoAgentMaintenanceService(
        _application_config(arguments)
    ).resume_workflow(
        thread_id=arguments.thread_id,
        repo=arguments.repo,
        project=arguments.project,
        approved=arguments.approve,
    )
    print(result.final_report or result.stop_reason)
    return 0 if result.status == "completed" else 2


def _handle_apply(arguments: argparse.Namespace) -> int:
    """把明确批准的已验证候选回写真实仓库。"""

    result = RepoAgentMaintenanceService(
        _application_config(arguments)
    ).apply(
        arguments.proposal_id,
        approved=arguments.approve,
    )
    print(f"候选 {result.proposal_id} 已回写真实仓库")
    print("修改文件：" + "、".join(result.changed_files))
    return 0


def _print_eval_report(report, output: str | None) -> int:
    """输出稳定 JSON 报告，并把评测状态映射为退出码。"""

    print(write_report(report, output))
    return 0 if report.passed else 2


def _handle_eval_retrieval(arguments: argparse.Namespace) -> int:
    """运行离线检索评测。"""

    report = evaluate_retrieval_cases(
        arguments.dataset,
        fixtures_root=arguments.fixtures_root,
        mode=arguments.mode,
    )
    return _print_eval_report(report, arguments.output)


def _handle_eval_explain(arguments: argparse.Namespace) -> int:
    """运行离线解释评测。"""

    report = evaluate_explain_cases(
        arguments.dataset,
        fixtures_root=arguments.fixtures_root,
    )
    return _print_eval_report(report, arguments.output)


def _handle_eval_patch(arguments: argparse.Namespace) -> int:
    """运行离线 Patch 评测。"""

    report = evaluate_patch_cases(
        arguments.dataset,
        fixtures_root=arguments.fixtures_root,
    )
    return _print_eval_report(report, arguments.output)


def _handle_migrate_state(arguments: argparse.Namespace) -> int:
    """执行 SQLite 到 PostgreSQL 状态迁移。"""

    report = migrate_state(
        sqlite_state_dir=arguments.sqlite_state_dir,
        postgres_dsn=arguments.postgres_dsn,
        execute=arguments.execute,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并把可预期错误转换为简洁的退出信息。"""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except KeyboardInterrupt:
        print("RepoAgent 已由用户中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"RepoAgent 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
