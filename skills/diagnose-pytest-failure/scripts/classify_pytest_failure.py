"""根据 pytest 进程观察执行确定性失败分类。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


RULES = (
    (
        "environment_failure",
        (
            r"ModuleNotFoundError",
            r"No module named",
            r"PermissionError",
            r"permission denied",
            r"connection refused",
            r"could not connect",
            r"No such file or directory",
            r"is not recognized as an internal or external command",
        ),
        "检查依赖锁定、解释器、权限和外部服务，再决定是否修改业务代码。",
    ),
    (
        "collection_failure",
        (
            r"ERROR collecting",
            r"Interrupted: \d+ error during collection",
            r"ImportError while importing test module",
            r"SyntaxError",
            r"fixture .+ not found",
        ),
        "定位首个收集异常，先恢复测试可收集状态，再分析业务断言。",
    ),
    (
        "assertion_failure",
        (
            r"AssertionError",
            r"E\s+assert\s",
            r"FAILED .+::",
            r"DID NOT RAISE",
        ),
        "读取失败断言及被测代码，比较期望与实际值并验证最小修复。",
    ),
)


def _signals(text: str, patterns: tuple[str, ...]) -> list[str]:
    """返回命中的有限原始行，保留可复核证据。"""

    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    matches: list[str] = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if any(pattern.search(normalized) for pattern in compiled):
            matches.append(normalized[:500])
        if len(matches) == 10:
            break
    return matches


def classify(payload: dict[str, object]) -> dict[str, object]:
    """根据工具状态、退出码和日志信号生成分类结果。"""

    stdout = str(payload["stdout"])
    stderr = str(payload["stderr"])
    exit_code = payload["exit_code"]
    timed_out = bool(payload["timed_out"])
    combined = f"{stdout}\n{stderr}"

    if timed_out:
        category = "tool_failure"
        signals = ["pytest 执行超时"]
        summary = "pytest 未在工具预算内结束，尚不能判断测试断言是否正确。"
        next_action = "缩小测试目标或检查阻塞、死锁和外部依赖后重试。"
    elif exit_code == 0:
        category = "passed"
        signals = _signals(combined, (r"\d+ passed", r"passed in")) or [
            "pytest 退出码为 0"
        ]
        summary = "pytest 正常结束且退出码为 0。"
        next_action = "保留测试目标和通过摘要；需要时继续执行合理回归范围。"
    else:
        category = "unknown_failure"
        signals = []
        summary = f"pytest 退出码为 {exit_code}，但现有信号不足以可靠分类。"
        next_action = "读取完整首个异常、pytest 摘要和测试配置后继续诊断。"
        for candidate, patterns, action in RULES:
            found = _signals(combined, patterns)
            if found:
                category = candidate
                signals = found
                next_action = action
                labels = {
                    "environment_failure": "环境或依赖层失败",
                    "collection_failure": "测试收集阶段失败",
                    "assertion_failure": "测试执行后的断言失败",
                }
                summary = f"检测到{labels[candidate]}，pytest 退出码为 {exit_code}。"
                break
        if not signals:
            signals = [f"pytest 退出码为 {exit_code}"]

    template = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "diagnosis-report-template.md"
    ).read_text(encoding="utf-8")
    report = (
        template.replace("{{category}}", category)
        .replace("{{summary}}", summary)
        .replace("{{signals}}", "；".join(signals))
        .replace("{{next_action}}", next_action)
    )
    return {
        "category": category,
        "summary": summary,
        "signals": signals,
        "recommended_next_action": next_action,
        "report_markdown": report,
    }


def main() -> int:
    """读取单个 JSON 对象并只输出一个 JSON 对象。"""

    payload = json.load(sys.stdin)
    result = classify(payload)
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
