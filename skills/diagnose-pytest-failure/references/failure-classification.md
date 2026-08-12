# pytest 失败分类规则

按以下优先级分类，避免同一输出被多个标签覆盖：

1. `tool_failure`：执行超时、进程无法启动、输出损坏或工具层错误。
2. `passed`：退出码为 0，并且没有工具层失败。
3. `environment_failure`：缺少依赖、权限、解释器、外部服务或平台能力。
4. `collection_failure`：语法错误、测试模块导入错误、fixture 注册失败或收集阶段中止。
5. `assertion_failure`：测试已经运行，但断言或预期异常不满足。
6. `unknown_failure`：退出码非零，但现有信号不足以可靠归类。

分类只描述失败发生在哪一层，不自动等于根因。例如 `ModuleNotFoundError` 可以确定为环境失败，但还需要读取依赖配置判断是缺包还是导入路径错误。
