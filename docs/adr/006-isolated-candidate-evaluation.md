# ADR-006：隔离候选工作副本与客观验证闭环

- 状态：Accepted
- 日期：2026-07-31
- 模块：Isolated candidate patch and objective evaluation

## 背景

Agent 已经能读取仓库、规划、恢复状态，但还不能安全地证明一个修改是否有效。直接修改用户目标仓库会让错误补丁污染真实工作区；只让模型阅读自己生成的 diff，又会把“看起来合理”误当成“能够编译并通过测试”。

因此需要一条确定性链路：复制目标仓库、只修改副本、验证变更范围、编译、运行目标测试和回归测试，最终把外部结果交给 Graph Evaluator。

## 决策

### 1. 创建独立 CandidateWorkspace

每次 run 在 RepoAgent 管理的工作目录下创建：

```text
candidate-workspaces/{run_id}/worktree
```

复制时：

- 跳过 `.git`、虚拟环境、缓存和 node_modules；
- 不复制符号链接；
- 限制文件数量、单文件大小和总字节数；
- 保存普通文件的基线 bytes；
- 使用保留原 project_id、但 repo_root 指向副本的 ProjectContext；
- 默认在上下文退出后删除副本。

工作副本目录禁止位于目标仓库内部，run_id 使用严格字符白名单，避免清理路径逃逸。

### 2. 第一版补丁使用完整文件替换

模型输出 CandidatePatch：

```text
patch_id
summary
changes[]:
  path
  expected_sha256
  replacement_content
  reason
```

完整文件替换比自行实现 unified diff parser 更容易建立确定语义。应用器只允许修改已有 UTF-8 文本和文件类型白名单，不支持创建、删除和重命名。

代价是大文件 token 成本更高。后续如果引入统一 diff 或结构化 range edit，仍需保留路径沙箱和旧哈希前置条件。

### 3. 使用旧内容哈希作为乐观并发前置条件

Agent 读取文件后，文件可能被用户、其他 Agent 或工具改变。写入前重新计算实际 SHA-256；与 `expected_sha256` 不一致则拒绝整个补丁。

所有变更先完成路径、文件类型、编码、哈希和 no-op 校验，再开始写入。任一前置条件失败时不会写入第一份文件；写入阶段出现 OSError 时尝试回滚已经写入的文件。

### 4. 验证顺序由风险和成本决定

```text
change_scope
  → python_compile
  → target_tests
  → regression_tests
```

- 范围错误：立即停止，不执行可能被未授权修改的项目代码。
- 编译失败：跳过全部测试，先修复最低成本的确定性错误。
- 目标测试失败：跳过回归，说明原问题尚未修好。
- 目标测试通过：再运行完整回归，检查局部修复是否破坏其他行为。

只有四项全部 passed，CandidateEvaluationReport 才通过。skipped 不算通过。

### 5. ObjectiveCandidateEvaluator 实现 Graph EvaluatorPort

Evaluator 在进入验证前检查 Graph 的 project_id 和 repo_revision 是否与工作副本基线一致。验证报告再转换成 EvaluationResult：

- `passed` 控制 Graph 是否进入 Report 或 Reflection；
- `issues` 保存失败检查和原因；
- `evidence` 保存变更文件及每项检查状态；
- 原始 CandidateEvaluationReport 保留 diff、命令、退出码和输出。

### 6. pytest 仍需显式代码执行授权

CandidateEvaluationConfig 默认 `allow_code_execution=False`。即使在副本中，pytest 仍会执行目标项目代码；未授权时编译可以继续，目标测试返回 evaluation error，回归测试跳过。

## 为什么测试通过仍不等于补丁正确

测试只证明执行到的样例没有失败，还可能存在：

- 测试覆盖不足；
- 修改范围过大；
- 性能、安全或兼容性退化；
- 测试本身断言错误；
- 未运行项目真实虚拟环境或外部依赖。

因此本模块同时检查 expected changed files、最大文件数、最大 diff 行数、编译、目标测试和回归。未来还可增加静态类型、lint、覆盖率、Reviewer 和人工审批。

## 没有选择的方案

### 直接修改真实目标仓库再回滚

进程崩溃、回滚失败或用户同时编辑时可能损坏真实工作区，因此拒绝。

### 一开始使用 git worktree

git worktree 节省复制空间并保留 Git 能力，但会修改源仓库的 `.git/worktrees` 元数据，需要处理非 Git 目录、脏工作区和清理失败。第一版使用普通文件副本，语义更统一。

### 让模型判断补丁是否正确

模型审查可以补充可读性和设计风险，但不能替代编译和测试等客观反馈。

### 收到一个自由文本 patch 后直接调用系统 patch 命令

自由 diff 需要处理路径前缀、offset、模糊匹配、创建删除和二进制语义。第一版先使用结构化完整文件替换，减少不确定解析和命令依赖。

## 代价与局限

- 普通复制会占用额外时间和磁盘，当前只适合小中型仓库。
- 完整文件替换比 range edit 消耗更多模型上下文。
- 第一版不能创建、删除或重命名文件。
- 内存保存基线 bytes，仓库预算当前为 100 MB。
- 工作副本只能隔离文件修改位置，不是 OS 进程沙箱。
- pytest 使用 RepoAgent 当前 Python 环境，尚未自动选择目标项目虚拟环境。
- 没有静态类型、lint、覆盖率和性能验证。

## 验证证据

测试覆盖：

- 缓存和虚拟环境不进入工作副本，退出后自动清理；
- 工作副本保留 project_id，但使用独立 repo_root；
- 候选修改和 unified diff 不改变真实仓库；
- 旧哈希冲突拒绝写入；
- 多文件补丁先完整校验，避免半补丁；
- 路径逃逸和非白名单文件类型拒绝；
- 真实 Python 编译、目标 pytest 和完整回归 pytest 通过；
- 编译失败时不执行项目代码；
- 未授权变更阻止编译和测试；
- 没有代码执行授权时测试不会运行；
- 目标测试失败与回归失败明确区分；
- ObjectiveCandidateEvaluator 接入 LangGraph EvaluatorPort；
- Graph 与工作副本 project/revision 不一致时拒绝评估；
- 工作副本复制前和测试后都执行资源预算；
- run_id 路径逃逸被拒绝。

全项目当前 86 个测试通过。

## 未来切换条件

- 大型 Git 仓库：改用临时 clone 或 git worktree，并实现可靠清理。
- 复杂编辑：引入带旧哈希的 range edit 或严格 unified diff parser。
- 不可信项目：在容器、受限账户和网络策略内运行 pytest。
- 项目环境差异：解析项目虚拟环境或使用声明式执行镜像。
- 应用真实修改：增加人工审批，把已验证 diff 应用到用户仓库。
