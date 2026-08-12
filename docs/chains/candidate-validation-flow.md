# 候选补丁客观验证链路

## 端到端流程

```text
显式 ProjectContext
  → 创建 RepoAgent 管理的 CandidateWorkspace
  → 复制普通文件并保存基线 bytes
  → ProjectContext.repo_root 切换到 worktree
  → CandidatePatch
       ├─ 相对路径
       ├─ 旧 SHA-256
       ├─ 完整新内容
       └─ 修改原因
  → 对全部 change 执行前置校验
       ├─ 路径仍在 worktree 内
       ├─ 已有普通文件
       ├─ 文件类型白名单
       ├─ UTF-8 文本
       ├─ 旧哈希匹配
       └─ 新旧内容不同
  → 只写入工作副本
  → 生成 unified diff
  → ObjectiveCandidateEvaluator
       ├─ change_scope
       ├─ python_compile
       ├─ target_tests
       └─ regression_tests
  → CandidateEvaluationReport
  → Workflow EvaluationResult
  → 通过则 Report；失败则 Reflection
```

## 验证短路

```text
范围失败
  → 拒绝执行项目代码

编译失败
  → 目标测试 skipped
  → 回归测试 skipped

目标测试失败
  → 回归测试 skipped

目标测试通过
  → 执行回归测试
```

短路既降低成本，也减少执行未授权或明显无效代码的风险。每个 skipped 都带原因，而且 skipped 不会被计为 passed。

## 路径隔离

```text
真实仓库 repo_root
  └─ 只读复制来源

RepoAgent state/candidate-workspaces/run-id/worktree
  ├─ 补丁写入
  ├─ 编译
  ├─ pytest
  └─ diff 生成
```

目标仓库与工作副本使用同一个 project_id，说明它们属于同一项目；repo_root 和 revision 不同，说明工具操作的环境和版本不同。

## 哈希冲突链路

```text
Agent 读取文件，得到 hash=A
  → 用户或其他工具修改文件，当前 hash=B
  → CandidatePatch.expected_sha256=A
  → 应用器重新读取，发现 B != A
  → 整个补丁拒绝
  → 重新读取并重新规划
```

旧哈希不是安全签名，而是乐观并发控制和 stale-context 检测。

## 评估结果如何进入 Graph

```text
CandidateEvaluationReport
  → passed
  → issues: failed/error checks
  → evidence:
       changed_file:src/calculator.py
       check:change_scope:passed
       check:python_compile:passed
       check:target_tests:passed
       check:regression_tests:passed
  → EvaluationResult
  → conditional edge
```

Graph 不解析 pytest 自由文本来决定路由；Evaluator 已把退出码和检查状态转换为稳定字段。
