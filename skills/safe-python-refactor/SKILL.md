---
name: safe-python-refactor
description: 对 Python 代码进行小步、可验证的安全重构；当任务涉及拆分函数、调整职责、清理重复代码或保持行为不变的结构优化时使用。
---

# Python 安全重构

先明确必须保持不变的外部行为、公共 API、异常语义和测试基线，再提出修改。

1. 用仓库搜索和 AST 检查定位定义、调用方、导入关系与测试覆盖。
2. 修改前后都调用 `analyze_python_api_change`，比较公共 API、函数签名和导入变化。
3. 每个候选补丁只解决一个结构问题，不夹带功能变化。
4. 在隔离候选工作区应用补丁，依次执行语法检查、相关测试和回归测试。
5. 测试通过不等于零风险；无法由测试覆盖的兼容性风险必须写入交付报告。

执行前读取 [重构检查表](references/refactor-checklist.md)；涉及公共接口时，再读取 [兼容性规则](references/compatibility-rules.md)。

