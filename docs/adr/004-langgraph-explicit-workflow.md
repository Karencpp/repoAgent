# ADR-004：使用 LangGraph 编排显式主闭环

- 状态：Accepted
- 日期：2026-07-31
- 模块：LangGraph main workflow

## 背景

最小 ReAct 适合在一个局部目标内根据 Observation 选择下一工具，但代码维护任务还需要保证全局步骤完整、执行后客观评估，并在失败时区分局部重试和重新规划。

如果把所有职责放进一个无限 while 循环，计划、当前步骤、失败证据和停止原因容易混进消息历史，条件跳转也难以单独测试。因此需要显式的外层状态机。

## 决策

使用 LangGraph StateGraph 编排六个业务节点：

```text
plan → execute_step → evaluate → reflect → replan → report
                ↑          │          │         │
                └──────────┴──────────┘         │
```

具体原则：

1. Graph State 使用 TypedDict 描述共享状态，节点返回局部更新。
2. 计划、步骤结果、评估和反思使用严格 Pydantic 模型。
3. 外层采用 Plan-and-Solve，单步骤内部复用 ReActExecutor。
4. Evaluator 是独立 Port，判断任务证据是否满足目标，不由 Executor 自评。
5. Reflection 只在评估失败后触发，默认最多一次。
6. Reflection 判断核心假设是否错误：局部错误进入 retry，计划错误进入 replan。
7. Replan 保留已完成前缀，替换失败步骤和未完成部分。
8. plan、evaluation、reflection 使用 reducer 追加历史，当前值可覆盖但历史不丢失。
9. 每个业务循环有显式预算，同时保留 LangGraph recursion_limit 作为最后保险。
10. report 节点根据结构化状态确定性生成报告，不再调用模型决定运行是否成功。

## 为什么 State 不只存 messages

消息历史适合模型上下文，却不适合承担业务状态：

- 无法稳定查询当前步骤和剩余预算；
- 需要从自然语言反推某步骤是否完成；
- 旧计划和新计划容易混淆；
- 不利于 checkpoint、条件路由和分层评测。

因此 State 单独保存 project identity、plan、step results、evaluation、reflection、预算计数、status、stop reason 和 trace。未来 Context Builder 再选择其中一部分转换为模型上下文。

## 为什么 TypedDict 与 Pydantic 同时使用

TypedDict 适合 LangGraph 的共享状态和节点局部更新，运行时开销小；Pydantic 适合模型或节点边界，能限制计划步骤数、唯一 id、字段类型和文本长度。

如果整个 Graph State 都使用 Pydantic，每个局部更新都可能带来额外验证和复制成本；如果全部使用自由 dict，则边界输出缺乏约束。两者分工比二选一更符合需求。

## 三种范式如何组合

```text
Plan-and-Solve：保证全局步骤完整
  └─ ReAct：在一个步骤内根据工具反馈局部探索
       └─ Observation：外部仓库证据

Evaluator：使用外部证据判断是否满足目标
  └─ Reflection：分析一次失败原因
       ├─ Retry：目标没错，只修正本步骤执行方式
       └─ Replan：原计划或核心假设错误，替换剩余步骤
```

Reflection 不是无条件“再想一次”，也不等于直接重试。它必须由客观失败触发，并产出可执行的纠正策略。

## 没有选择的方案

### 继续扩展手写 while 循环

对于模块三的局部 ReAct，普通 Python 循环最清楚。但外层出现多个角色、条件分支、回边和未来持久化需求后，StateGraph 更容易检查拓扑、观察节点状态和接入 checkpoint。

### 所有节点都交给一个模型

模型可以生成计划、选择工具和分析失败，但状态转移、次数上限、是否允许重规划和最终成功状态必须由程序控制，否则无法稳定评测。

### 每次失败都 Replan

参数不准或一次搜索失败通常只需局部 Retry。无条件 Replan 会丢弃有效步骤、增加模型调用，并可能把小问题升级为全局漂移。

### 只依赖 LangGraph recursion_limit

recursion_limit 只能在总超步数到达后硬停止，无法表达“只反思一次”“只重规划一次”等业务语义，也无法生成精确停止原因。因此它只是兜底。

## 代价与局限

- 当前 Planner、Evaluator、Reflector 只有 Port 和脚本测试替身，真实模型适配后续接入。
- pytest、编译和 diff 范围的 ObjectiveCandidateEvaluator 已在模块六实现。
- SQLite checkpointer 已在模块五接入；本 ADR 保留的是主图本身的设计边界。
- 当前同步串行执行，没有并行步骤和取消机制。
- Replan 保留已完成前缀，但不自动判断旧结论是否因新证据失效。
- report 是最小确定性报告，尚未做 Evidence 引用整理。

## 验证证据

测试覆盖：

- 两步计划按顺序执行并生成成功报告；
- Planner 获取显式 project_id、repo_root 和 revision；
- 计划步骤数量和 id 唯一性；
- Planner、Executor、Evaluator、Reflector、Replanner 异常收敛；
- 局部失败经过一次 Reflection 后 Retry；
- 计划不完整时 Replan 并保留完成前缀；
- Reflection 和 Replan 预算为零时不越界调用；
- 第二次评估仍失败时停止，不无限反思；
- 步骤结果 id 不匹配时拒绝污染状态；
- 真实 ReActExecutor 能作为 Graph 的 StepExecutor；
- 六个业务节点确实存在于编译图中；
- 计划、评估和反思历史不会因覆盖当前值而丢失。

全项目当前 61 个测试通过。

## 未来切换条件

- 需要断点恢复：模块五已按 project/thread 隔离接入 SQLite checkpointer。
- 需要真实决策：为 Planner、Evaluator、Reflector 实现结构化模型适配器。
- 需要客观验证：模块六已接入 pytest、compile、diff 范围检查的确定性 Evaluator。
- 需要人工审批：在 fix 模式写操作前增加 interrupt 和 resume。
- 需要并行探索：只对无依赖只读步骤使用 Send，并定义结果 reducer 和总预算。
