# 失败案例：检索到的内容全部塞进上下文

## 现象

系统把对话历史、十几条 Memory、二十个 RAG Chunk 和全部工具输出无条件加入 Prompt。模型窗口尚未超限，但回答开始忽略最新失败测试，反而采用旧版本记忆中的实现方式。

## 根因

- 混淆 Retrieval 与最终 Context；
- 没有 token 预算和输出预留；
- 没有去重、优先级和版本过滤；
- 系统指令、运行状态和外部 Evidence 混在同一自由文本中；
- 认为上下文越多越好。

## 当前处理

- 所有来源转换为 ContextPacket；
- mandatory 与 optional 分开；
- 先去重，再按 priority 填充 token 预算；
- 给模型输出单独预留 token；
- 固定信任分区并转义外部内容的分区标签；
- 每个 Packet 保存 included、duplicate 或 budget_exceeded；
- Memory 和 RAG 仍需 project/revision 过滤。

## 面试结论

Context Engineering 的目标不是收集最多信息，而是在有限窗口内交付当前决策最需要、来源清楚且风险分区明确的信息。

