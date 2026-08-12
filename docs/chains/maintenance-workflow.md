# Maintenance Workflow

```text
repo-agent fix
  |
  v
SQLiteMaintenanceWorkflowRuntime
  |
  v
RepoAgentMaintenanceWorkflow
  |
  +--> analyze_repository
  +--> select_targets
  +--> propose_patch
  +--> evaluate_patch
          |
          +-- passed --> persist_proposal --> await_approval -- approved --> promote_patch
          |
          +-- failed --> reflect_patch -- repair --> propose_patch
                               |
                               +-- reselect --> select_targets
                               +-- stop --> report_failure
```

`await_approval` uses LangGraph interrupt/resume. The CLI approval path is:

```powershell
repo-agent fix --repo D:\code\target "fix objective" --allow-code-execution
repo-agent resume-fix --repo D:\code\target --thread-id <thread-id> --approve
repo-agent resume-fix --repo D:\code\target --thread-id <thread-id> --reject
```

The compatibility path remains:

```powershell
repo-agent apply --proposal-id <proposal-id> --approve
```
