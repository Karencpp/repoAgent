# ADR 012: LangGraph Maintenance Loop

Date: 2026-08-07

## Status

Accepted for the local CLI implementation.

## Context

The previous `fix` path ran read-only analysis and then executed a single patch
pipeline outside the workflow graph. That made proposal creation auditable, but
test failures could not drive structured reflection and a second patch attempt.

## Decision

Add a separate `RepoAgentMaintenanceWorkflow` instead of merging patch state into
the read-only Diagnose graph.

The graph owns these nodes:

```text
analyze_repository -> select_targets -> propose_patch -> evaluate_patch
evaluate_patch passed -> persist_proposal -> await_approval
evaluate_patch failed -> reflect_patch -> propose_patch/select_targets
await_approval approved -> promote_patch -> report_success
await_approval rejected -> report_rejected
```

Patch generation, evaluation, reflection, proposal persistence, and promotion are
accessed through ports. Existing candidate modules remain the implementation for
workspace copy, patch application, pytest evaluation, and atomic promotion.

## Consequences

- Failed objective evaluation can now feed `reflect_patch` and produce a second
  patch attempt.
- Approval is checkpointed with LangGraph `interrupt()` and can be resumed after
  reopening the SQLite runtime.
- The existing `apply --proposal-id ... --approve` command remains compatible
  because proposals are still persisted in the old JSON format.
- The first implementation does not add Docker isolation, PostgreSQL, pgvector,
  FastAPI, or workers.
