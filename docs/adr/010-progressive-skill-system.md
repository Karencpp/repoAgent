# ADR-010：可执行、可版本化且不扩权的 Skill v2 能力包

- 状态：Accepted
- 日期：2026-08-03
- 模块：Agent Skills

## 背景

只有 `SKILL.md` 和参考资料的第一版能复用 Prompt，但无法承载确定性处理、输入输出契约、自测数据和依赖声明。pytest 失败分类、Python 公共 API 对比如果完全交给 LLM，不稳定且难以回归验证。因此 Skill 需要从“说明书”升级成受控能力包，同时继续保证 Skill 不是权限来源，动态任务进度也不写入静态 Skill。

## 决策

### 1. 采用开放入口加项目 Manifest

每个包仍以 `SKILL.md` 为入口，frontmatter 只保留 `name` 和 `description`。RepoAgent 使用 `skill.yaml` 声明包版本、mode、路由信息、工具上限、参考资料、资产、测试、依赖和脚本契约。

这种设计避免把大量机器配置塞进 Markdown，同时保持入口格式对通用 Skill 客户端友好。`skill.yaml` 是项目扩展，不宣称是所有厂商共同规范。

### 2. Skill 静态，State 动态

Skill 保存可复用的操作规程和确定性能力，不保存“当前执行到第几步”。计划、观察、重试、当前步骤和激活 Skill 快照进入 LangGraph State，并由 SQLite Checkpoint 持久化。

### 3. 渐进披露

- 发现：读取 frontmatter 与 Manifest，产生不含正文的 Descriptor；
- 激活：命中任务后读取 `SKILL.md` 和 `instruction_resources`；
- 执行：脚本、Schema、资产只在工具调用或脚本内部使用，不注入上下文。

这同时控制 token、无关指令干扰和脚本暴露面。

### 4. 脚本映射为普通 Tool

Manifest 中每个脚本必须声明唯一工具名、描述、入口文件、输入 Schema、输出 Schema、超时和输出上限。启动时宿主校验 Schema 并注册工具；调用时先校验当前激活 Skill，再校验输入，使用 JSON stdin/stdout 启动独立 Python 进程，最后验证输出。

子进程使用 `shell=False`、隔离 Python 模式、显式 UTF-8、最小环境变量、超时和输出限制，不继承模型 API Key。由于它不是完整 OS 沙箱，只有显式可信根中已审核的脚本可以运行，系统不自动安装依赖，也不执行目标仓库自带 Skill。

### 5. Skill 不扩权

模型最终能看到的工具仍是：

```text
registered ∩ workflow_step_allowed ∩ skill_allowed
```

Manifest 可以使脚本在 Registry 中“存在”，但不能越过 Step 白名单。`required_tools` 表示宿主必须安装这些能力；它只做环境完整性检查，不替当前步骤授权。

### 6. 完整包指纹

内容哈希覆盖 `SKILL.md`、`skill.yaml`、所有声明的 references、scripts、schemas、assets、tests 和 `agents/openai.yaml`。`StepExecution` 保存 name、SemVer、包 hash 与路由原因。恢复或后续步骤发现同名 Skill 漂移时停止，而不是静默混用新旧版本。

### 7. 依赖只校验不安装

激活时验证 Python 最低版本和本地包是否可导入。Agent 不在任务中自动 `pip install`，避免供应链风险和不可重复环境变更。

## 已实现的两个能力包

- `diagnose-pytest-failure`：把 pytest 结果确定性分类为通过、断言失败、收集失败、环境失败、工具失败或未知失败，并生成诊断报告。
- `safe-python-refactor`：用 AST 比较重构前后的公共函数、类方法、签名和导入变化，给出兼容性风险；它只提供结构证据，不能代替测试。

两个包都包含入口、Manifest、两份参考资料、脚本、双向 JSON Schema、报告模板、自测用例和展示元数据。

## 当前局限

- 路由仍是可解释的词法规则，没有 Embedding 或模型 rerank；
- 每个 ReAct Step 只激活一个主 Skill；
- 子进程不是容器或 OS 级文件系统/网络沙箱；
- 没有远程 Registry、签名、撤销和审批链；
- 能力包自测已具备，但尚未建立真实任务的版本 A/B 评测。

## 验证

测试覆盖 v2 发现、参考资料激活、Schema 参数拒绝、错误 Skill 作用域拒绝、两个脚本的能力包用例，以及任一声明文件改变后的快照漂移检测。

