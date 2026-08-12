# ReAct vs LangGraph orchestration pilot

This pilot isolates orchestration behavior on 12 read-only diagnosis tasks. It is
not evidence for automated patch success.

## Controlled variables

- Use the same GLM chat model and model configuration.
- Use the same repository revision, read-only tool registry, and system context.
- Disable Skill, Memory, MCP, and RAG for both arms in this pilot.
- Give both arms the same total tool-call budget per task.
- ReAct-only receives the complete user goal once.
- LangGraph uses the current Plan -> Execute -> Evaluate -> Reflect -> Replan
  graph, with the same ReAct executor inside each Execute node.
- Run each task three times. Preserve every raw event and model response.

## Independent pass criteria

A run passes only when all of the following are true:

1. Every `required_paths` entry appears in successful tool evidence.
2. The answer contains at least one normalized phrase from every
   `required_fact_groups` group.
3. The run ends normally and does not exceed the tool-call budget.

The evaluator must operate after both arms finish and must not use an LLM.

## Reported metrics

- Task Success Rate, overall and by `difficulty`.
- Required-path evidence recall.
- Tool calls and invalid/repeated tool calls per successful task.
- End-to-end latency and LLM calls per task.
- Replan rate and failed evaluation count for LangGraph.

Compare quality under an equal tool-call budget, then report latency and LLM
calls separately. Equalizing LLM calls would remove the mechanism being tested
because planning, evaluation, and reflection are additional model operations.

## Expansion gate

Only expand to 30-50 tasks after the pilot can be replayed without evaluator
ambiguity. The next suite should use fixed Git commits, executable tests, and
diff constraints for objective patch validation.
