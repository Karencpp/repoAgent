# ADR-001：显式 ProjectContext 与稳定项目身份

- 状态：Accepted
- 日期：2026-07-31
- 模块：Project selection and isolation

## 背景

RepoAgent 自身源码仓库与被维护的目标代码库不是同一个对象。用户可能在任意目录启动程序，也可能在多个 Python 项目之间切换。后续工具、Memory、RAG、Checkpoint 和 Trace 都必须知道“当前针对哪个项目、哪个版本”。

如果默认使用进程当前目录，会产生两个问题：一是可能误读或误写 RepoAgent 自己；二是项目切换后容易把上一个项目的状态带到下一个项目。

## 决策

1. 每次 Run 必须显式提供 `repo` 路径或已注册 `project`，且只能提供一个。
2. 未提供选择时失败，不使用 `cwd` 兜底；空字符串同样失败。
3. 注册项目获得随机、稳定的 `project_id`；路径移动后通过 `update_path` 保持身份。
4. 临时路径使用规范绝对路径的哈希生成 `adhoc project_id`，不写入注册表。
5. 每次 Resolve 重新检查当前 revision，项目身份与代码版本分开建模。
6. Memory、RAG 和 Checkpoint namespace 都从 `project_id` 派生。
7. `repo_root` 是后续工具的沙箱边界；即使选择的是 monorepo 子目录，也不自动扩大到 Git 根目录。
8. 注册表使用带 schema version 的 JSON，并通过同目录临时文件加 `os.replace` 原子更新。

## 为什么项目身份不能只用绝对路径

路径是位置，不是业务身份。仓库移动盘符或目录后，如果直接把路径当 ID，会丢失历史 Memory 和索引归属。注册项目的稳定 `project_id` 允许更新路径而不改变身份。

## 为什么 revision 不能等于 project_id

同一项目会不断提交、切换分支和产生未提交修改。Memory 需要长期归属于项目，但代码索引和 Evidence 必须知道具体版本。把二者混在一起会导致：

- 每次提交都变成一个“新项目”；或
- 项目 ID 不变时错误复用旧版本索引。

当前实现对 Git 仓库记录 commit，并区分 clean/dirty；dirty 状态附加工作区 manifest 指纹。非 Git 目录使用 manifest 指纹。

## 没有选择的方案

### 默认使用当前工作目录

优点是 CLI 简短。缺点是目标隐式、不可审计，容易选错安全边界，因此拒绝。

### 只用 Git remote 作为 ID

本地仓库可能没有 remote；fork、多个 worktree 和 monorepo 子目录也不能仅靠 remote 区分。

### 在每个目标仓库写 `.repo-agent/project.json`

可以让身份随仓库移动，但会污染目标项目，并要求 RepoAgent 在只读诊断前先写文件。第一版选择外部注册表。

### 一开始使用 SQLite 注册表

SQLite 更适合并发和查询，但当前注册表规模很小、单进程使用，JSON 更容易人工查看。Memory 模块仍计划使用 SQLite。若出现多进程写入，再迁移注册表或增加文件锁。

## 代价与局限

- JSON 注册表目前没有跨进程文件锁，只保证单次替换原子性。
- 非 Git manifest 基于路径、大小和修改时间，是缓存失效提示，不是内容真实性证明。
- dirty Git revision 表示“需要刷新”，不用于恢复工作区内容。
- 注册项目移动后需要用户显式更新路径。
- 名称当前限制为 ASCII 安全字符，便于 CLI、路径和 namespace 使用。

## 验证证据

模块测试覆盖：

- 缺失、空和冲突选择；
- 临时 ID 稳定但不落注册表；
- 两项目 namespace 隔离；
- 重名、重复路径和非法别名；
- 路径移动后 ID 保持；
- Git clean/dirty revision；
- monorepo 子目录保持为工具沙箱；
- `..` 和绝对外部路径逃逸；
- 注册表 schema 和 entry 损坏时失败关闭。

## 未来切换条件

- 多进程或守护服务：迁移 SQLite 或增加跨进程锁。
- 远程仓库自动克隆：增加 clone source、credential policy 和 workspace lifecycle。
- 多 worktree 精细识别：增加 Git common-dir、worktree id 和 branch metadata。
- 大型非 Git 目录：将 revision 检查改为增量文件监控或索引器维护。

