"""基于可审计规则选择 Skill，不把路由结果当作执行权限。"""

from __future__ import annotations

import re

from .models import SkillDescriptor, SkillRouteMatch


_WORD_PATTERN = re.compile(r"[a-z0-9_-]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_GENERIC_TAGS = {"python"}


def _tokens(value: str) -> set[str]:
    """提取英文词和连续中文文本用于轻量匹配。"""

    return {item.casefold() for item in _WORD_PATTERN.findall(value)}


class SkillRouter:
    """对元数据做确定性打分，保留每个加分原因。"""

    def route(
        self,
        user_goal: str,
        skills: tuple[SkillDescriptor, ...],
        *,
        mode: str | None = None,
        limit: int = 3,
        min_score: int = 10,
    ) -> tuple[SkillRouteMatch, ...]:
        """返回稳定排序的候选 Skill，不自动加载正文。"""

        normalized_goal = user_goal.strip().casefold()
        if not normalized_goal:
            raise ValueError("user_goal 不能为空")
        if limit < 1:
            raise ValueError("limit 必须大于等于 1")
        goal_tokens = _tokens(normalized_goal)
        matches: list[SkillRouteMatch] = []
        for skill in skills:
            if mode is not None and mode not in skill.modes:
                continue
            score = 0
            reasons: list[str] = []
            normalized_name = skill.name.casefold()
            spaced_name = normalized_name.replace("-", " ")
            if normalized_name in normalized_goal or spaced_name in normalized_goal:
                score += 100
                reasons.append("任务直接出现 Skill 名称")
            for trigger in skill.triggers:
                if trigger.casefold() in normalized_goal:
                    score += 30
                    reasons.append(f"命中触发词：{trigger}")
            for tag in skill.tags:
                if tag.casefold() in normalized_goal:
                    score += 4 if tag.casefold() in _GENERIC_TAGS else 12
                    reasons.append(f"命中标签：{tag}")
            description_overlap = goal_tokens.intersection(
                _tokens(skill.description)
            )
            if description_overlap:
                overlap_score = min(20, len(description_overlap) * 4)
                score += overlap_score
                reasons.append(
                    "描述词重合：" + ", ".join(sorted(description_overlap))
                )
            if score >= min_score:
                matches.append(
                    SkillRouteMatch(
                        skill=skill,
                        score=score,
                        reasons=tuple(reasons),
                    )
                )
        matches.sort(key=lambda item: (-item.score, item.skill.name))
        return tuple(matches[:limit])
