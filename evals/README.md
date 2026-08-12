# RepoAgent Offline Evals

These datasets are small, deterministic fixtures. They do not call a live LLM and
do not copy private user repositories.

Commands:

```powershell
repo-agent eval retrieval --dataset evals/retrieval/python-small.jsonl
repo-agent eval explain --dataset evals/explain/python-small.jsonl
repo-agent eval patch --dataset evals/patch/python-small.jsonl
```

Live model evaluation must be run separately and explicitly; default evals use
local fixtures and local feature-hash embeddings only.
