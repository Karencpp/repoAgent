# 失败案例：把旧版本 RAG 命中当成当前代码事实

## 现象

索引是在 commit A 建立的，用户随后修改了目标仓库。Agent 在 commit B 的任务里仍从旧索引读到某个函数，并据此生成补丁；实际文件已经重命名或逻辑发生变化。

更隐蔽的情况是：目标目录位于另一个 Git 仓库的 ignored 子目录中。系统只读取父仓库 clean commit，于是子目录内容改变后 revision 仍不变。

## 根因

- 只用 project_id 隔离，没有绑定 repo revision；
- 建库开始后代码发生变化，结果却仍标记为开始时的 revision；
- 错误地认为父 Git commit 一定能代表所选子目录；
- RAG 命中没有来源行号，无法通过当前文件复核。

## 当前处理

- 索引状态保存 project_id、repo_revision、Embedding 模型和维度；
- 建库开始和 Embedding 结束后各检查一次 revision；
- 检索发现 revision 不一致时 fail closed；
- 父 Git 中没有 tracked file 的所选子目录使用 manifest 指纹；
- 每个命中返回路径、行号、内容哈希和 revision；
- Agent 再使用当前仓库的精确读取工具验证。

## 面试结论

RAG 是派生索引，不是真实数据源。任何缓存和索引都必须说明自己对应哪个项目、哪个版本，以及怎样验证仍然新鲜。

