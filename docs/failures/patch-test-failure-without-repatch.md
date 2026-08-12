# Failure Case: Patch Test Failure Without Repatch

## Problem

A single-shot patch pipeline can produce a candidate, run pytest, and persist a
failure report, but the failed stdout/stderr never becomes structured input for a
new candidate. This creates a dead end: the system can explain why the patch
failed but cannot repair it in the same auditable run.

## Guardrail

`RepoAgentMaintenanceWorkflow` routes failed `evaluate_patch` results into
`reflect_patch` while attempts remain. The reflection only consumes objective
evaluation evidence, including pytest stdout/stderr, and returns one of:

- `repair`
- `reselect`
- `stop`

Duplicate patch fingerprints stop the loop before another evaluation attempt.
