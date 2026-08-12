# 模块 04 面试讲解：LangGraph 主闭环

## 30 秒回答

我用 LangGraph 把外层任务建模为 Plan、Execute、Evaluate、Reflect、Replan、Report 六个显式节点。Planner 保证全局步骤，单步骤内部复用 ReAct；Evaluator 用外部证据判断任务级成功，失败后 Reflection 只执行一次，并区分局部 Retry 和计划级 Replan。状态不是只有 messages，而是保存项目身份、计划、步骤结果、评估、预算和停止原因。条件边和上限都由确定性程序控制，旧计划与旧评估通过 reducer 保留历史。

## 2 分钟回答

上一模块的 ReActExecutor 适合解决一个局部步骤，但纯 ReAct 在长任务里容易边探索边改变目标，所以我在外层增加 Plan-and-Solve。Planner 最多生成六步，每步说明目标、期望证据和工具白名单。Execute 节点按顺序执行步骤，实际执行器复用受限 ReAct。

全部步骤完成或某一步失败后进入 Evaluator。Evaluator 与 Executor 分离，因为“模型完成回答”不等于“任务通过测试或证据校验”。评估失败才允许进入 Reflection。Reflection 输出失败原因、纠正动作和是否需要 Replan：如果只是参数或搜索范围不对，就重试当前步骤；如果原计划缺步骤或核心假设错误，就保留已完成前缀并替换剩余计划。

循环由程序限制，默认最多一次 Reflection 和一次 Replan，LangGraph recursion limit 只做最后兜底。State 使用 TypedDict 承担共享状态，Pydantic 校验计划、评估等边界对象；plan history、evaluation history、step results 使用 reducer 追加。这样可以从 Trace 还原为什么重试、为什么重规划、为什么最终停止。

## 最重要的组合关系

```text
Plan-and-Solve：全局完整性
  → ReAct：单步骤局部适应
  → Evaluator：外部证据判断
  → Reflection：分析失败原因
  → Retry 或 Replan：修改执行策略或全局计划
```

这些概念不是多个模型角色的简单堆叠，而是不同粒度的控制职责。

## 面试官可能追问

### 为什么要 LangGraph，普通 while 不行吗？

局部 ReAct 用 while 更清楚，所以模块三没有强行上框架。外层出现六个节点、多个条件分支、回边、状态历史和未来 checkpoint 后，图可以显式表达拓扑并单测每条路由。LangGraph 是编排工具，不提高模型智力。

### 为什么 State 不直接用 messages？

messages 是模型上下文，不是业务数据库。从消息里反推当前步骤、评估状态、剩余预算和旧计划既脆弱又难评测。Graph State 保存结构化事实，Context Builder 再决定哪些事实进入下一次模型调用。

### 为什么 Graph State 用 TypedDict，计划又用 Pydantic？

TypedDict 适合节点读取全状态、返回局部更新；Pydantic 适合不可信边界，能校验步骤数、唯一 id 和字段长度。整个 State 都用 Pydantic会增加局部更新成本，全部用 dict 又缺少边界约束。

### Reducer 是什么，项目中怎么用？

节点通常覆盖普通字段；带 reducer 的字段会把旧值和新值合并。本项目内部用 list 加法追加 step results、trace、计划历史和评估历史，对外结果再转成不可变 tuple。这样既兼容 checkpoint 序列化，又不丢状态迁移证据。

### Retry、Reflection、Replan 有什么区别？

- Retry：目标和计划没错，只重新执行同一步，通常先修正参数。
- Reflection：读取客观失败证据，判断根因和纠正策略。
- Replan：核心假设或步骤结构错误，替换未完成计划。

Reflection 是决策过程，Retry/Replan 是后续动作。

### 为什么 Reflection 只做一次？

反复让同一模型分析同一失败很容易没有信息增量。代码任务有测试和工具证据，一次修正后仍失败就应停止并暴露局限，或者由人工提供新信息。次数是配置，不是写在 Prompt 里的愿望。

### 为什么 Evaluator 不能由 Executor 兼任？

Executor 容易把自己的输出当成功。Evaluator 应使用独立标准，例如测试退出码、编译结果、diff 范围和预期 Evidence。职责分离也便于定位究竟是执行失败还是验收规则失败。

### Replan 为什么保留已完成前缀？

已经由证据支持的步骤不应无条件重做，否则增加成本并可能产生漂移。但“保留”不是永久正确，后续如果新证据推翻旧结论，需要让 Replanner 明确使对应步骤失效。

### 为什么既有业务预算，又有 recursion limit？

业务预算能表达精确语义和停止原因，例如最多反思一次；recursion limit 只防御代码 bug 或意外回路，是最后保险，不能替代业务规则。

### LangGraph Checkpoint 和 Memory 是一回事吗？

不是。Checkpoint 保存某个 thread 的运行状态，用于暂停和恢复；Memory 保存跨任务仍有价值的项目事实和经验。SQLite checkpointer 已在模块五接入。

### 为什么报告节点不再调用 LLM？

运行状态、步骤结果和评估已经结构化，是否成功应由程序决定。模型以后可以润色报告，但不能改变 completed/failed、测试结果或停止原因。

### 当前 Evaluator 真的跑 pytest 了吗？

本模块先稳定 Evaluator Port 和路由契约；模块六已经提供真实 pytest、编译和 diff 范围检查的 ObjectiveCandidateEvaluator。Planner 和 Reflector 仍使用结构化 Port 与测试替身。

## 当前代码证据

- `workflow/models.py`：计划、执行、评估、反思、Trace 和运行结果模型。
- `workflow/ports.py`：四类角色端口与 ReAct 适配器。
- `workflow/graph.py`：六节点状态图、条件边、预算和报告。
- `workflow/fakes.py`：确定性 Planner/Executor/Evaluator/Reflector。
- `tests/test_workflow_graph.py`：15 个主图分支测试。

全项目当前 61 个测试全部通过。

## 主动说明的局限

1. Planner、Evaluator、Reflector 尚未连接真实模型。
2. 客观 Evaluator 已在模块六实现，但尚未增加静态类型、lint 和覆盖率规则。
3. SQLite checkpoint 和 interrupt/resume 已在模块五加入，但还没有人工审批界面。
4. 当前步骤串行执行，没有并行分支。
5. Replan 不会自动使已经完成但后来被推翻的步骤失效。
6. 最终报告只做确定性汇总，还没有完整 Evidence 引用。
