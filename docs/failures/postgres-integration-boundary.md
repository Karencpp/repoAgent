# PostgreSQL 集成边界不能用 Mock 冒充

## 现象

PostgreSQL/pgvector 后端需要数据库扩展、HNSW 索引、FTS 和事务语义。只用内存假对象通过测试，无法证明 ANN 查询没有在 Python 中全量扫描，也无法证明生命周期操作和索引更新在同一事务中完成。

## 防护

- 默认离线测试只验证端口、配置和 SQLite 兼容性。
- PostgreSQL 真实集成测试必须显式提供 DSN 和已迁移数据库。
- 报告中区分离线契约测试和真实 PostgreSQL 集成验证。
- `migrate-state` 不转换 Checkpoint，也不声称旧 JSON proposal 已进入数据库。

## 处理

资源不足或 Docker 不可用时，记录未执行真实 PostgreSQL 集成测试的原因，不能把 Mock 结果写成真实 PostgreSQL 指标。
