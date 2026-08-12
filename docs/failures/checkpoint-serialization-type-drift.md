# 失败案例：Checkpoint 恢复后的 reducer 类型漂移

## 现象

内存运行时，追加型 Graph channel 使用 tuple，reducer 是 tuple 加法。写入 SQLite 再恢复后，msgpack 将裸 tuple 还原成 list；下一节点返回 tuple 更新时出现：

```text
TypeError: can only concatenate list (not "tuple") to list
```

原有全部内存测试通过，只有关闭连接并用新 Runtime 恢复时才暴露。

## 根因

代码把 Python 内存类型等同于持久化后的表示形式，却没有做真正的序列化往返测试。

## 当前处理

- Graph 内部追加型 channel 统一使用 list reducer；
- 节点更新也返回 list；
- 进入 Port 时转换成 tuple，保持领域请求只读；
- 最终 RepoAgentRunResult 由 Pydantic 转成 tuple；
- 测试必须关闭第一个 SQLite 连接，再由新 Runtime 恢复。

## 面试结论

“对象能够 `model_dump_json`”不等于整个状态机能够 checkpoint round-trip。持久化测试必须覆盖实际 serializer、数据库和新进程式重建。
