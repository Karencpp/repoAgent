# Project selection and isolation flow

## 目标

把用户的一次显式目标选择转换成不可变的 `ProjectContext`，作为整条 Agent Graph 的项目身份、版本信息、命名空间和路径沙箱。

## 注册项目链路

```text
repo path + display name
  → 校验名称
  → 规范化绝对路径
  → 检查目录存在
  → 读取 Git/manifest revision
  → 检查重名与重复路径
  → 分配稳定 project_id
  → 原子写入 registry
  → 返回 ProjectRegistration
```

## Run 解析链路

```text
repo? / project?
  ├─ 都没有 → ProjectSelectionRequiredError
  ├─ 都提供 → AmbiguousProjectSelectionError
  ├─ repo
  │    → inspect current path
  │    → path hash 生成 adhoc id
  │    → 不持久化
  └─ project
       → 从 registry 找到稳定 id 和当前路径
       → 重新 inspect revision
       → refresh last_seen_revision

→ 派生 memory/rag/checkpoint namespace
→ 创建 immutable ProjectContext
```

## 项目身份与版本

```text
project_id
  表示长期业务身份
  用于 Memory、RAG、Checkpoint 隔离

revision
  表示本次看到的代码版本
  用于索引失效、Evidence 新鲜度和 Trace
```

## 路径沙箱链路

```text
tool candidate path
  → 相对路径与 repo_root 拼接；绝对路径保持原值
  → resolve 规范化并跟随已有 symlink
  → 检查是否仍是 repo_root 的后代
       ├─ 否 → PathOutsideRepositoryError
       └─ 是 → 根据 must_exist 检查存在性
  → 返回工具可用绝对路径
```

## 切换项目

切换不是修改全局 `current_repo`，而是为新 Run 创建新的 `ProjectContext`。后续模块不得从进程全局变量读取项目身份；必须从 Graph State 或显式参数接收 Context。

这条规则可以防止：

- 并发 Run 相互覆盖当前项目；
- 上一个项目的 Memory 进入下一个项目；
- 工具缓存按路径或全局 key 误复用；
- Trace 无法还原当时的仓库和版本。

