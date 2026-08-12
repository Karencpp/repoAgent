"""比较 Python 源码重构前后的公共结构差异。"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}({ast.unparse(node.args)})"


def _public_api(tree: ast.Module) -> dict[str, str]:
    api: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            api[node.name] = _signature(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            api[node.name] = "class"
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("_"):
                    api[f"{node.name}.{child.name}"] = _signature(child)
    return api


def _imports(tree: ast.Module) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            result.update(f"{module}:{alias.name}" for alias in node.names)
    return result


def _parse(source: str) -> tuple[ast.Module | None, str | None]:
    try:
        return ast.parse(source), None
    except SyntaxError as error:
        location = f"第 {error.lineno or '?'} 行"
        return None, f"{location}存在语法错误：{error.msg}"


def _render_list(values: list[str]) -> str:
    return "、".join(values) if values else "无"


def _render_changes(values: list[dict[str, str]]) -> str:
    if not values:
        return "无"
    return "；".join(f"{item['name']}: {item['before']} -> {item['after']}" for item in values)


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    path = str(payload["path"])
    before_tree, before_error = _parse(str(payload["before_source"]))
    after_tree, after_error = _parse(str(payload["after_source"]))

    before_api = _public_api(before_tree) if before_tree else {}
    after_api = _public_api(after_tree) if after_tree else {}
    before_imports = _imports(before_tree) if before_tree else set()
    after_imports = _imports(after_tree) if after_tree else set()

    added_api = sorted(after_api.keys() - before_api.keys())
    removed_api = sorted(before_api.keys() - after_api.keys())
    changed = [
        {"name": name, "before": before_api[name], "after": after_api[name]}
        for name in sorted(before_api.keys() & after_api.keys())
        if before_api[name] != after_api[name]
    ]
    added_imports = sorted(after_imports - before_imports)
    removed_imports = sorted(before_imports - after_imports)

    risk_flags: list[str] = []
    if before_error:
        risk_flags.append(f"修改前源码无法解析：{before_error}")
    if after_error:
        risk_flags.append(f"修改后源码无法解析：{after_error}")
    if removed_api:
        risk_flags.append("删除了公共 API，现有调用方可能失效")
    if changed:
        risk_flags.append("公共函数或方法签名发生变化")
    if removed_imports or added_imports:
        risk_flags.append("导入依赖发生变化，需要检查运行环境与副作用")
    if added_api:
        risk_flags.append("新增了公共 API，需要确认是否属于本次重构范围")

    if before_error or after_error or removed_api or changed:
        risk_level = "high"
    elif added_api or added_imports or removed_imports:
        risk_level = "medium"
    else:
        risk_level = "low"

    summary = {
        "low": "未发现公共结构变化，仍需通过测试验证运行时行为",
        "medium": "发现非破坏性结构变化，需要确认影响范围并运行回归测试",
        "high": "发现语法问题或潜在破坏性接口变化，不应直接交付",
    }[risk_level]
    template_path = Path(__file__).resolve().parents[1] / "assets" / "refactor-report-template.md"
    template = template_path.read_text(encoding="utf-8")
    report = template.format(
        risk_level=risk_level,
        summary=summary,
        added_public_api=_render_list(added_api),
        removed_public_api=_render_list(removed_api),
        changed_signatures=_render_changes(changed),
        added_imports=_render_list(added_imports),
        removed_imports=_render_list(removed_imports),
        risk_flags=_render_list(risk_flags),
    )
    return {
        "path": path,
        "syntax_valid_before": before_tree is not None,
        "syntax_valid_after": after_tree is not None,
        "added_public_api": added_api,
        "removed_public_api": removed_api,
        "changed_signatures": changed,
        "added_imports": added_imports,
        "removed_imports": removed_imports,
        "risk_level": risk_level,
        "risk_flags": risk_flags,
        "summary": summary,
        "report_markdown": report,
    }


def main() -> None:
    payload = json.load(sys.stdin)
    json.dump(analyze(payload), sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()

