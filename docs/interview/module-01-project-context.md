# 模块 01 面试讲解：ProjectContext 与多代码库隔离

## 30 秒回答

RepoAgent 自身和目标代码库是两个仓库，所以我没有默认使用当前目录，而是要求每次显式传 `repo` 或注册项目别名。注册项目有稳定 `project_id`，用于隔离后续的 Memory、RAG 和 Checkpoint；每次运行再读取 commit 和 dirty 状态，区分项目身份与代码版本。最终生成不可变 `ProjectContext`，其中 `repo_root` 也是所有工具的路径沙箱，防止切换项目后串库或越界读取。

## 2 分钟回答

这个模块解决的不是简单的路径参数，而是 Agent 的多项目状态边界。最直接的方案是默认读取 cwd，但用户可能从 RepoAgent 源码目录或任意目录启动，这会让目标变成隐式状态，也容易把上一个项目的 Memory、索引和工具缓存带到下一个项目。

我的设计是每个 Run 必须显式选择目标：临时路径得到基于规范路径的 adhoc ID；常用仓库注册后得到稳定随机 `project_id`。稳定 ID 解决仓库移动后长期记忆归属问题，而 revision 解决同一项目在不同 commit、分支和 dirty 工作区下的索引新鲜度问题。两者不能合并。

Resolver 最终返回不可变 ProjectContext，里面包含 repo root、revision、Git 元数据和 Memory/RAG/Checkpoint namespace。后续 Graph Node 不读取全局 current repo，只接收 Context。工具解析路径时必须确认规范路径仍位于 repo root 内；即使选的是 monorepo 子目录，也不会自动扩大权限到 Git 根目录。

第一版注册表使用带版本的 JSON，因为规模小、需要人工可读，并通过临时文件加原子替换避免半写入。它还没有跨进程锁，这是我明确保留的限制；如果做服务化或并发写入，会迁移 SQLite 或增加锁。

## 最重要的设计逻辑

```text
路径是位置
project_id 是长期身份
revision 是本次代码版本
namespace 是存储隔离
repo_root 是工具权限边界
```

## 面试官可能追问

### 为什么不能直接把绝对路径当 project_id？

仓库移动后路径会变，但它仍是同一个业务项目。路径适合定位，稳定 ID 适合长期 Memory 和索引归属。

### 为什么不用 Git remote 当 ID？

本地项目可能没有 remote；fork、多 worktree 和 monorepo 子目录也可能共享或缺少 remote。remote 可以作为辅助元数据，不能单独承担身份。

### project_id 和 commit SHA 有什么区别？

project_id 跨版本稳定，回答“这是哪个项目”；commit/revision 回答“这次看到哪个代码状态”。前者用于隔离长期状态，后者用于索引失效和证据新鲜度。

### dirty 工作区怎么办？

revision 标记为 dirty 并附加 manifest 指纹，提示下游索引刷新。当前实现不保存完整 dirty 内容，不能把 revision 当作恢复快照。

### 非 Git 目录怎么办？

使用文件路径、大小和修改时间生成 manifest 指纹。它是缓存失效提示，不是密码学内容证明；后续索引器仍要给单个文件生成内容哈希。

### monorepo 选择子目录时为什么不自动升到 Git 根目录？

自动升根会悄悄扩大 Agent 可读取和修改的范围。Git 元数据可以来自上层根，但工具沙箱必须保持用户显式选择的子目录。

### JSON 注册表会不会并发写坏？

当前通过同目录临时文件和 `os.replace` 保证单次更新不会留下半个 JSON，但没有解决多个进程的 lost update。第一版是单进程 CLI；并发出现时迁移 SQLite 或增加文件锁。

### 路径沙箱只检查 `..` 够吗？

不够。绝对路径和 symlink 都可能绕过字符串检查，所以先做文件系统 `resolve`，再检查解析后的路径是否属于 repo root。

### 为什么 ProjectContext 要不可变？

如果运行中修改 repo root 或 project ID，前后工具结果和持久化状态就不再属于同一个安全域。切换项目应该创建新 Context 和新 Run，而不是修改共享对象。

### 注册项目移动了怎么办？

使用 `update_path` 修改注册位置，但保留 project_id。下次 Resolve 读取新 revision，长期状态仍属于原项目。

### 为什么没有项目参数时不友好地使用 cwd？

这是安全和可解释性取舍。显式失败比静默选择错误仓库更容易发现，也让 Trace 能证明目标来自用户选择而非启动环境。

## 当前代码证据

- `ProjectContextResolver.resolve`：显式二选一和 Context 创建。
- `ProjectRegistry`：稳定 ID、注册、路径更新和原子持久化。
- `inspect_repository`：Git/非 Git revision。
- `ProjectContext.resolve_repo_path`：工具路径沙箱。
- `tests/test_projects.py`：16 个行为与失败分支测试。

## 主动说明的局限

1. 还没有 CLI 命令，只完成了核心领域接口。
2. JSON 注册表没有跨进程锁。
3. 非 Git manifest 不读取完整内容。
4. 还没有把 namespace 接入真实 Memory/RAG/Checkpoint。
5. 尚未处理远程 clone、凭证和 worktree 生命周期。

主动讲出这些局限不会减分，反而说明你知道模块当前承诺的边界。

