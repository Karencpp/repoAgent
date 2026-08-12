# 模块 10 面试讲解：Agent Skill v2

## 30 秒标准回答

我把 Skill 设计成静态、可版本化的能力包，而不只是 Markdown Prompt。入口 `SKILL.md` 告诉模型何时使用、按什么流程做；`skill.yaml` 声明路由、依赖、工具和资源；references 提供细则，scripts 做确定性操作，JSON Schema 约束输入输出，assets 和 tests 分别保存模板与自测数据。系统按“目录发现、任务激活、按需执行”渐进加载。Skill 不能授予权限，脚本注册成普通 Tool，仍受 Step 白名单、参数校验和受控子进程约束；动态进度保存在 Agent State 和 SQLite Checkpoint，不写回 Skill。

## 2 分钟标准回答

Skill 解决的是“同类任务应该怎么稳定完成”，Tool 解决的是“系统实际能执行什么”。第一版只有 Markdown 时，流程知识可以复用，但像 pytest 失败分类、重构前后 API 比较仍由 LLM 自由判断，不够稳定。所以我把两个 Skill 重写成完整能力包。

包的入口仍是通用的 `SKILL.md`，只放 name、description 和精炼工作流；项目扩展的 `skill.yaml` 保存 SemVer、mode、trigger、allowed tools、依赖和脚本契约。详细规则放 references，确定性逻辑放 scripts，输入输出分别由 JSON Schema 限制，报告格式放 assets，典型用例放 tests。这样 Markdown 负责指导推理，脚本负责可重复计算。

加载是渐进的：启动只扫描轻量元数据；任务命中后才读取正文和指令参考；脚本与资产只在真正调用时使用。安全上，目标仓库中的 Skill 不会自动成为可信指令。脚本只能在对应 Skill 激活的调用链内执行，还要经过当前 Step 白名单、输入 Schema、超时、输出上限和输出 Schema。子进程不继承 API Key，但我会明确说它还不是容器级沙箱，因此只能运行已审核的内置脚本。

静态能力包和动态状态是分开的。当前计划、工具观察、重试进度保存在 Graph State/SQLite Checkpoint；Step 只记录本次激活 Skill 的 name、version、整个包 hash 和路由原因。恢复时如果任何声明文件变了就停止，避免一次任务混用两版能力。

## 两个真实例子

`diagnose-pytest-failure` 接收 pytest 的 stdout、stderr、exit code 和 timed_out。脚本按确定性优先级区分断言、收集、环境、工具和未知失败，输出结构化类别、信号、下一步与 Markdown 报告。LLM 再结合代码证据解释根因，而不是自己随意猜失败类型。

`safe-python-refactor` 接收修改前后源码，用 AST 收集公共函数、类方法、签名与导入，输出新增/删除 API、签名变化和风险等级。它能发现结构兼容性风险，但 AST 不能证明运行时语义相同，因此仍必须跑相关测试和回归测试。

## 高频追问标准答案

### Skill 和 Prompt 模板有什么区别？

Prompt 模板主要复用一段文本；Skill 是有发现、路由、版本、资源、工具契约、测试和恢复语义的能力包。Markdown 只是入口，不是全部。

### Skill 和 Tool 有什么区别？

Skill 是程序性知识，说明顺序、判断标准和边界；Tool 是原子执行能力。Skill 中的脚本也必须注册成 Tool 后才能调用，不能绕开 Registry。

### Skill 和 MCP 有什么关系？

Skill 描述怎么做，MCP 标准化跨进程能力怎么被发现和调用。Skill 可以编排 MCP Tool，MCP 也可以分发资源，但远程 Skill 仍要进入本项目的审核、安装、版本和可信根流程。

### 为什么脚本不直接让模型运行？

模型只产生结构化 tool call。Host 校验工具名和参数后，以固定入口、JSON stdin/stdout、非 shell 子进程运行脚本，并验证返回 Schema。这样脚本调用可审计、可限制、可测试。

### 受控子进程是否等于沙箱？

不等于。当前实现限制了 shell、时间、输出和环境变量，也不传 API Key，但没有 OS 级文件系统与网络隔离。因此可信根和代码审核仍是主要信任边界；更高安全等级应使用容器、低权限账号或系统沙箱。

### allowed_tools 和 required_tools 怎么理解？

allowed_tools 是这个 Skill 最多允许暴露的工具范围，它只能收窄 Workflow 权限。required_tools 是宿主安装完整性检查，说明缺少这些工具时包不完整；它不会替某一步自动授权。

### 为什么 references 会进入上下文，assets 和 tests 不进入？

references 是模型做判断需要的规程细节。assets 是脚本或输出使用的模板，tests 是能力包回归数据，把它们全部放进 Prompt 只会浪费 token。Manifest 显式区分用途。

### 依赖为什么只检查不自动安装？

任务执行中自动安装依赖会改变环境并引入供应链风险。包声明 Python 和依赖要求，宿主激活前校验，安装由受控部署流程完成。

### 为什么要版本号和完整包哈希？

SemVer 表达作者声明的变化语义，hash 捕获实际内容变化。hash 覆盖入口、Manifest、脚本、Schema、参考资料、模板与测试，作者漏升版本也能发现漂移。

### Skill 的执行进度放在哪里？

Skill 是跨任务复用的静态定义，不应该被一次任务污染。动态的 plan、当前 step、observation、retry 和 active skill snapshot 都在 LangGraph State，由 SQLite Checkpoint 持久化。

### 当前方案还有哪些不足？

路由是词法打分、单 Step 只激活一个 Skill、脚本隔离还不是 OS 沙箱、缺少签名和远程发布审批，也没有真实任务的 Skill A/B 评测。我会把这些作为明确的工程边界，而不是声称已经做到生产级平台。

## 代码证据

- `skills/*/SKILL.md` 与 `skill.yaml`：入口和包声明。
- `skills/*/{references,scripts,schemas,assets,tests}`：完整能力包内容。
- `src/repo_agent/skills/catalog.py`：可信发现、依赖校验、包哈希与资源加载。
- `src/repo_agent/skills/scripts.py`：脚本 Tool 注册、作用域、子进程和双向 Schema。
- `src/repo_agent/skills/runtime.py`：路由、激活并接入 ReAct。
- `src/repo_agent/workflow/models.py`：Skill 快照随 Step 进入 Checkpoint。
- `tests/test_skill_system.py`：两个内置包的真实执行和安全回归测试。
