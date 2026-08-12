# 受限仓库工具调用链路

## 完整 Function Calling 链路

```text
Host 将工具名称、说明和输入 Schema 放入模型上下文
  → 模型生成 tool name + arguments
  → Host 查找工具目录
  → 参数 Schema 校验
  → 权限与副作用策略校验
  → RepositoryToolPort 调用具体实现
  → ProjectContext 校验路径沙箱
  → 本地函数或受限子进程执行
  → ToolResult 返回结构化数据或错误
  → Host 将结果作为 Observation 写入 Agent 状态
  → 模型决定继续调用、完成步骤或请求重规划
```

模型没有直接执行 Python 函数，也没有绕过 Host 获得终端权限。

## 只读工具链路

```text
结构化输入
  → 检查长度、数量、glob 和行范围
  → 路径 resolve
  → 确认仍位于 repo_root
  → 执行有限读取/搜索/AST 解析
  → 返回路径、行号、截断和结果上限元数据
```

输出上限不是简单丢弃信息。结果必须告诉上层是否截断或达到上限，否则模型会把“不完整观察”误认为“完整事实”。

## pytest 执行链路

```text
RunPytestInput
  → 检查显式代码执行授权
  → 校验 target、keyword、max_failures、timeout、output_limit
  → target path 通过 ProjectContext 沙箱
  → 程序构造固定参数数组
  → SecureSubprocessRunner(shell=False, cwd=repo_root)
  → 超时控制与 stdout/stderr 头尾截断
  → ProcessResult
       ├─ exit_code == 0：工具成功，测试通过
       ├─ exit_code != 0：工具成功，测试失败 Observation
       ├─ timed_out：TIMEOUT 工具错误，保留部分输出
       └─ launch_error：EXECUTION_ERROR 工具错误
```

## 为什么需要 RepositoryToolPort

Graph Node 不应知道工具是本地 Python、MCP Server 还是远程服务。Node 只依赖相同的输入输出语义：

```text
LocalRepositoryTools ─┐
                      ├─ RepositoryToolPort ─ Graph Node
MCPRepositoryTools   ─┘
```

Port 的价值不是“为了抽象而抽象”，而是让协议替换不改变 Agent 状态机，并允许使用测试替身稳定验证 Planner/Executor。

