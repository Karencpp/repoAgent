---
name: diagnose-pytest-failure
description: 诊断 Python 项目的 pytest 失败并形成可复核报告；当任务涉及 pytest、单元测试失败、收集异常、依赖或环境错误、超时以及回归失败时使用。
---
# pytest 失败诊断

完成门槛：获得 pytest 输出后，必须分别用 `read_file_range` 读取失败测试和直接被测源码，并调用 `classify_pytest_failure`；三类证据不完整时不得结束。

1. 先读取失败测试、被测代码和已有日志的最小相关片段，不要扫描整个仓库。
2. 只有当前步骤授权 `run_pytest` 时才运行最小测试目标；未授权时明确说明证据限制。
3. 获得 pytest 退出码与输出后，调用 `classify_pytest_failure` 做确定性分类，不凭印象判断。
4. 根据已加载的失败分类规则区分断言、收集、环境、工具和未知失败。
5. 根因结论必须引用测试节点、退出码、首个关键异常以及代码文件行号；证据不足时标记为假设。
6. 修复后先运行最小相关测试，再扩大到合理回归范围。
7. 使用脚本生成的报告结构组织结论，不把“工具成功返回”写成“测试通过”。

需要核对细节时使用 [失败分类规则](references/failure-classification.md) 和 [证据要求](references/evidence-requirements.md)。
