# 模块 06 面试讲解：隔离候选补丁与客观 Evaluator

## 30 秒回答

RepoAgent 不直接修改用户仓库，而是把目标仓库普通文件复制到每个 run 独立的 CandidateWorkspace。模型输出路径、旧 SHA-256 和完整新内容；Host 先校验所有路径、文件类型、编码和哈希，再只写工作副本并生成 unified diff。Evaluator 按变更范围、Python 编译、目标测试、完整回归的顺序验证，任何 skipped 都不算通过。最终结构化 EvaluationResult 决定 LangGraph 是完成还是进入 Reflection。

## 2 分钟回答

写代码 Agent 最大的问题不是能不能生成 diff，而是如何避免错误修改真实仓库，以及如何证明修改有效。我没有让模型直接写当前 repo_root，而是为每个 run 创建独立工作副本。复制时跳过 `.git`、虚拟环境、缓存和符号链接，并限制文件数和总大小；副本继承 project_id，但有自己的 repo_root 和 candidate revision。

补丁第一版使用完整文件替换。每个 change 必须带读取时的 SHA-256，写入前 Host 重新计算实际哈希。如果用户或其他工具已经改变文件，整个补丁直接冲突，不会覆盖新代码。所有文件先校验再写，所以不会因为第二个文件失败留下半个补丁。

验证按成本和风险短路：先检查 changed files 和 diff 范围；范围异常时绝不执行项目代码。然后 compile 所有 Python 文件；通过后跑能证明原问题修复的 target tests；最后跑完整 regression。只有四项全通过才返回 passed。Evaluator 实现 LangGraph 端口，因此失败会触发外层 Reflection，而不是让 Executor 自己宣布成功。

## 面试官可能追问

### 为什么不直接在真实仓库修改，失败再 git checkout？

进程崩溃、非 Git 目录、未提交用户改动和回滚失败都可能造成数据损坏。候选副本让失败默认可丢弃，真实仓库只在未来人工批准后接收已验证 diff。

### 为什么第一版不用 git worktree？

worktree 空间效率高，但会修改源仓库 Git 元数据，并要求处理 Git 状态、清理和非 Git 仓库。普通复制对所有目录语义一致，更适合先建立安全和评估链路。大型仓库再切换。

### 为什么补丁用完整文件内容，不用 unified diff？

完整替换没有 offset、模糊匹配和 path prefix 解析歧义，容易验证旧哈希和做确定性测试。缺点是 token 成本高、不能优雅处理大文件。后续可以换 range edit，但不能删除路径和哈希边界。

### expected_sha256 解决什么？

解决读取和写入之间的 lost update。它证明当前文件仍是 Agent 生成补丁时看到的版本。它不是数字签名，也不自动解决冲突。

### 多文件补丁是数据库意义上的原子事务吗？

不是。当前先验证全部文件，再顺序写入；OSError 时尽力回滚。它能避免校验失败造成半补丁，但断电级原子性需要临时文件、文件系统事务策略或 Git commit/worktree。

### 为什么先检查 diff 范围，再运行测试？

未授权文件发生变化时，测试通过也不能接受；而且修改后的项目代码可能危险。范围检查成本最低，也决定后面代码是否值得执行。

### target tests 和 regression tests 有什么区别？

Target tests 证明用户报告的问题被修复；Regression tests 检查修复没有破坏其他行为。目标测试不通过就没必要运行更昂贵的回归。

### pytest 退出码 1 是工具错误吗？

不是。进程正常返回失败断言，工具成功提供 Observation；Evaluator 把对应 check 标记为 failed。超时、启动失败或未授权才是 error。

### 测试全通过能证明补丁正确吗？

不能，只能提高置信度。还要考虑覆盖率、diff 范围、类型检查、性能、安全、测试质量和人工审查。本项目明确不把 passed 等同于数学证明。

### 工作副本是否等于沙箱？

不等于。它防止 RepoAgent 正常路径工具写入真实仓库，但 pytest 中的 Python 代码仍有当前进程权限。执行不可信项目需要容器、受限账户、网络和资源策略。

### 为什么 Evaluator 要检查 project_id 和 revision？

防止 Graph 在项目 A 的任务中误用项目 B 的候选副本，也防止旧基线补丁被用于新 revision。这与 Checkpoint 恢复前的版本校验是同一原则：Evidence 必须绑定环境版本。

### 真实 LLM 测试应该放进普通单元测试吗？

不应该。正常回归必须确定、离线、无密钥且成本稳定。真实模型调用适合作为显式 opt-in 集成测试，验证供应商协议和少量端到端样例；输出仍要经过 Schema、工具、补丁和 Evaluator 边界。

## 当前代码证据

- `candidate/workspace.py`：受预算约束的副本、基线和差异扫描。
- `candidate/models.py`：补丁、检查和评估模型。
- `candidate/patching.py`：路径、哈希、编码、批量前置校验和 diff。
- `candidate/evaluator.py`：编译、目标测试、回归测试与 Workflow Adapter。
- `tests/test_candidate_evaluation.py`：15 个候选修改测试，其中包含真实 pytest。

全项目当前 86 个测试全部通过。

## 主动说明的局限

1. 工作副本使用普通复制，不适合超大型仓库。
2. 补丁只能完整替换已有白名单文本文件。
3. 多文件写入不是断电级原子事务。
4. 工作副本不是 OS 进程沙箱。
5. pytest 使用当前解释器，没有自动选择目标虚拟环境。
6. 尚未加入 mypy、ruff、覆盖率、性能和安全扫描。
7. 尚未实现人工批准后把验证补丁应用到真实仓库。
