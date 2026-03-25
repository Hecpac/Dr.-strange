# HEC-7: Pipeline End-to-End de Codigo Autonomo — Design Spec

**Date:** 2026-03-24
**Status:** Approved
**Scope:** Autonomous code pipeline: Linear issue → branch → code generation → tests → approval checkpoint → PR

---

## 1. Context

Claw v2.1 has a functioning runtime with LLM routing, brain service, agents, git worktree isolation, GitHub PR creation, approval manager, and cron scheduling. This feature adds an autonomous code pipeline that receives issues from Linear and produces PRs.

## 2. Approach

**Pipeline as orchestrator, reusing existing primitives.** Two new files:
- `linear.py` (~80 lines) — thin wrapper around Linear MCP tools
- `pipeline.py` (~220 lines) — `PipelineService` orchestrating the full flow

Reuses: `LLMRouter` (code generation), git worktree pattern (isolation), `GitHubPullRequestService` (PR creation), `ApprovalManager` (checkpoint gate), `CronScheduler` (polling).

## 3. Design

### 3.1 linear.py — Linear MCP Wrapper (~80 lines)

```python
@dataclass(slots=True)
class LinearIssue:
    id: str              # "HEC-123"
    title: str
    description: str
    state: str           # "Todo", "In Progress", etc.
    labels: list[str]
    branch_name: str     # Linear's suggested branch name
    url: str

class LinearService:
    def __init__(self, mcp_caller: Callable):
        ...

    def list_actionable(self, label="claw-auto", state="Todo") -> list[LinearIssue]
    def get_issue(self, issue_id: str) -> LinearIssue
    def update_status(self, issue_id: str, state: str) -> None
    def post_comment(self, issue_id: str, body: str) -> None
    def link_pr(self, issue_id: str, pr_url: str, pr_title: str) -> None
```

`mcp_caller` is an injected callable — production uses MCP tools, tests use mocks.

### 3.2 pipeline.py — PipelineService (~220 lines)

State machine per issue:
```
DISCOVERED → IN_PROGRESS → CODE_GENERATED → TESTS_PASSED → AWAITING_APPROVAL → PR_CREATED → DONE
                              ↓ (fail)          ↓ (fail, retry ≤3)
                            FAILED             RETRY → CODE_GENERATED
```

```python
@dataclass(slots=True)
class PipelineRun:
    issue_id: str
    branch_name: str
    repo_root: str
    status: str
    worktree_path: str | None = None
    diff: str | None = None
    test_output: str | None = None
    pr_url: str | None = None
    approval_id: str | None = None
    approval_token: str | None = None
    retries: int = 0

class PipelineService:
    def __init__(
        self,
        linear: LinearService,
        router: LLMRouter,
        approvals: ApprovalManager,
        pull_requests: GitHubPullRequestService,
        observe: ObserveStream | None,
        default_repo_root: Path,
        max_retries: int = 3,
        state_root: Path,       # ~/.claw/pipeline/
    ): ...

    def process_issue(self, issue_id, *, repo_root=None) -> PipelineRun
    def complete_pipeline(self, issue_id, approval_id) -> PipelineRun
    def poll_actionable(self) -> list[PipelineRun]
    def list_active(self) -> list[PipelineRun]
```

**process_issue flow:**
1. Fetch issue from Linear via `linear.get_issue()`
2. Update Linear status to "In Progress"
3. Create git branch from issue (e.g., `feat/HEC-123-issue-title`)
4. Create git worktree for isolation
5. Send issue description + codebase context to worker lane for code generation
6. Run `pytest` in worktree via subprocess
7. If tests fail and retries < 3: loop back to step 5 with error context
8. If tests pass: post summary comment on Linear, create approval via `ApprovalManager`
9. Return `PipelineRun` with status `AWAITING_APPROVAL`

**complete_pipeline flow:**
1. Verify approval via `ApprovalManager`
2. Commit changes in worktree, push branch
3. Create PR via `GitHubPullRequestService`
4. Link PR on Linear issue, update status to "In Review"
5. Clean up worktree
6. Return `PipelineRun` with status `PR_CREATED`

**State persistence:** `PipelineRun` objects saved as JSON in `~/.claw/pipeline/{issue_id}.json`. Survives restart — runs in `AWAITING_APPROVAL` can be completed after reboot.

### 3.3 Bot Commands

Added to existing `bot.py`:

- `/pipeline HEC-123` — Manual trigger, optional repo path as second arg
- `/pipeline_approve <approval_id> <token>` — Complete the checkpoint, create PR
- `/pipeline_status` — List active pipeline runs

### 3.4 Cron Integration

Added to `main.py` `build_runtime()`:
```python
scheduler.register(ScheduledJob(
    name="pipeline_poll",
    interval_seconds=300,
    handler=pipeline.poll_actionable,
))
```

### 3.5 Config Additions

```python
# AppConfig new fields:
pipeline_repo_root: Path | None     # PIPELINE_REPO_ROOT, None = workspace_root
pipeline_label: str                 # PIPELINE_LABEL, default "claw-auto"
pipeline_max_retries: int           # PIPELINE_MAX_RETRIES, default 3
```

## 4. Non-Goals

- Webhook receiver (polling only, single-process architecture)
- Auto-merge after PR approval (manual merge by human)
- Custom Linear fields for repo path (use Telegram command override)
- TTS for pipeline notifications

## 5. Test Plan

| Test file | Coverage |
|-----------|----------|
| `test_linear.py` (~80 lines) | Mock MCP caller; list_actionable filtering, get_issue parsing, update_status, post_comment |
| `test_pipeline.py` (~150 lines) | Mock deps; full pipeline happy path, retry on test failure (max 3), approval checkpoint, complete_pipeline creates PR, poll_actionable filtering, state persistence |

## 6. Files Summary

| Action | File | Lines (est.) |
|--------|------|-------------|
| Create | `claw_v2/linear.py` | ~80 |
| Create | `claw_v2/pipeline.py` | ~220 |
| Edit | `claw_v2/config.py` | +6 |
| Edit | `claw_v2/main.py` | +10 |
| Edit | `claw_v2/bot.py` | +40 |
| Edit | `.env` | +4 |
| Create | `tests/test_linear.py` | ~80 |
| Create | `tests/test_pipeline.py` | ~150 |

## 7. Constraints

- All new Python files under 250 lines
- bot.py will reach ~453 lines (pre-existing tech debt, refactor deferred)
- config.py stays at ~142 lines
- Existing 79 tests must continue passing
