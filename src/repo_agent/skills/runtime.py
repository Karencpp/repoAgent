"""把 Skill 激活结果接入现有 ReAct 控制循环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from repo_agent.react.runtime import ReActExecutor, ReActRunResult
from repo_agent.tools.registry import ToolRegistry

from .catalog import SkillCatalog
from .models import ActivatedSkill, SkillRouteMatch
from .routing import SkillRouter
from .scripts import skill_script_scope


class SkillManager:
    """协调目录、路由、激活和资源读取。"""

    def __init__(
        self,
        catalog: SkillCatalog,
        tool_registry: ToolRegistry,
        *,
        router: SkillRouter | None = None,
    ) -> None:
        self.catalog = catalog
        self.tool_registry = tool_registry
        self.router = router or SkillRouter()

    def route(
        self,
        user_goal: str,
        *,
        mode: str | None = None,
        limit: int = 3,
    ) -> tuple[SkillRouteMatch, ...]:
        """只基于轻量元数据返回候选项。"""

        return self.router.route(
            user_goal,
            self.catalog.descriptors(),
            mode=mode,
            limit=limit,
        )

    def activate(
        self,
        name: str,
        *,
        runtime_allowed_tools: Iterable[str] | None = None,
        mode: str | None = None,
    ) -> ActivatedSkill:
        """激活一个 Skill；当前版本每个 ReAct 步骤只采用一个主 Skill。"""

        return self.catalog.activate(
            name,
            self.tool_registry,
            runtime_allowed_tools=runtime_allowed_tools,
            mode=mode,
        )


@dataclass(frozen=True, slots=True)
class SkillAwareRunResult:
    """同时保留 Skill 激活审计信息和原始 ReAct 结果。"""

    active_skill: ActivatedSkill | None
    route_matches: tuple[SkillRouteMatch, ...]
    react_result: ReActRunResult


class SkillAwareReActExecutor:
    """在 ReAct 前完成元数据路由、正文激活和工具权限收窄。"""

    def __init__(
        self,
        react_executor: ReActExecutor,
        skill_manager: SkillManager,
    ) -> None:
        if react_executor.tool_registry is not skill_manager.tool_registry:
            raise ValueError("SkillManager 与 ReActExecutor 必须共享同一 ToolRegistry")
        self.react_executor = react_executor
        self.skill_manager = skill_manager

    def run(
        self,
        user_goal: str,
        *,
        skill_name: str | None = None,
        mode: str | None = None,
        system_instructions: str = "",
        allowed_tools: Iterable[str] | None = None,
        auto_route: bool = True,
        runtime_required_tools: Iterable[str] = (),
        runtime_required_tool_counts: Mapping[str, int] | None = None,
    ) -> SkillAwareRunResult:
        """显式名称优先；否则可选择确定性路由得分最高的 Skill。"""

        matches: tuple[SkillRouteMatch, ...] = ()
        selected_name = skill_name
        if selected_name is None and auto_route:
            matches = self.skill_manager.route(user_goal, mode=mode)
            if matches:
                selected_name = matches[0].skill.name

        active: ActivatedSkill | None = None
        effective_tools = (
            tuple(sorted(set(allowed_tools))) if allowed_tools is not None else None
        )
        skill_instructions: tuple[str, ...] = ()
        if selected_name is not None:
            active = self.skill_manager.activate(
                selected_name,
                runtime_allowed_tools=effective_tools,
                mode=mode,
            )
            effective_tools = active.effective_tools
            skill_instructions = (active.render_instructions(),)

        with skill_script_scope(
            active.descriptor.name if active is not None else None
        ):
            required_tools = set(runtime_required_tools)
            if active is not None:
                required_tools.update(active.descriptor.required_tools)
            result = self.react_executor.run(
                user_goal,
                system_instructions=system_instructions,
                allowed_tools=effective_tools,
                skill_instructions=skill_instructions,
                required_tools=tuple(sorted(required_tools)),
                required_tool_counts=runtime_required_tool_counts,
            )
        return SkillAwareRunResult(
            active_skill=active,
            route_matches=matches,
            react_result=result,
        )
