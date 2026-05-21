# SKILLS ROUTER

This file is the single entrypoint for coding agents or IDE agents that need a `SKILLS.md` file before using the deeper skill folders.

Use this file to choose the correct skill folder quickly. After choosing, open the matching `SKILL.md` inside that subfolder and follow its workflow.

This file is intended to live in the target repository root together with:

```text
AGENTS_PR_RULE.md
AGENTS_POST_PR_RULE.md
codex-security\
superpowers\
humanizer\
```

These repo-local files and folders are the source of truth. Do not rely on global paths outside the active repository workspace.

Before any audit, implementation, post-PR work, or reviewer-comment work, verify these files/folders exist in the repository root:

```text
AGENTS_PR_RULE.md
AGENTS_POST_PR_RULE.md
SKILLS.md
codex-security\
superpowers\
humanizer\
```

If any of them are missing, STOP immediately and ask the user:

```text
Apakah file dan folder ada?
```

Do not continue using global paths, web search, inferred skill behavior, or partial skill access when these repo-local files/folders are missing.

## Skill Roots

Security skills:

```text
<repo-root>\codex-security\63976030\skills
```

Superpowers development skills:

```text
<repo-root>\superpowers\63976030\skills
```

Human-friendly writing / PR description support:

```text
<repo-root>\humanizer
```

## Quick Decision Table

| Task type | Use this first | Why |
| --- | --- | --- |
| Full repository security audit | `codex-security/security-scan/SKILL.md` | Top-level orchestrator for threat model, discovery, validation, and attack-path analysis. |
| Security PR idea discovery | `codex-security/security-scan/SKILL.md` | Use for repository-wide security review before proposing security PRs. |
| Validate a suspected vulnerability | `codex-security/validation/SKILL.md` | Use after there is already a candidate finding to prove or disprove it. |
| Explain exploitability / severity | `codex-security/attack-path-analysis/SKILL.md` | Use after a finding is plausible or validated. |
| Fix a validated security issue | `codex-security/fix-finding/SKILL.md` | Use when the user asks to implement and verify a security fix. |
| Build a new feature | `superpowers/brainstorming/SKILL.md`, then `superpowers/test-driven-development/SKILL.md` | Clarify feature behavior first, then implement with tests. |
| Implement a selected PR idea | `superpowers/test-driven-development/SKILL.md` | Write/adjust tests before implementation when behavior changes. |
| Debug a broken workflow or failing test | `superpowers/systematic-debugging/SKILL.md` | Find root cause before patching. |
| Address reviewer comments | `superpowers/receiving-code-review/SKILL.md` | Verify reviewer feedback against code reality before changing code. |
| Write a multi-step implementation plan | `superpowers/writing-plans/SKILL.md` | Use before touching code for larger or multi-file changes. |
| Execute an existing plan | `superpowers/executing-plans/SKILL.md` or `superpowers/subagent-driven-development/SKILL.md` | Use subagent-driven development only when the environment supports subagents. |
| Verify before saying done | `superpowers/verification-before-completion/SKILL.md` | Required before claiming work is complete or checks pass. |
| Finish branch / prepare PR handoff | `superpowers/finishing-a-development-branch/SKILL.md` | Use after implementation and tests pass. |
| Generate friendly PR descriptions | `humanizer` | Use the repo-local humanizer folder to make descriptions natural and non-robotic. |

## Codex Security Skills

### `security-scan`

Use for full security scans of a repository, branch, PR, commit, patch, or working-tree diff.

This is the preferred starting point for deep security audit work. It orchestrates:

1. `threat-model`
2. `finding-discovery`
3. `validation`
4. `attack-path-analysis`
5. final markdown output

Do not skip straight to later phases for a full scan.

### `threat-model`

Use when you need to define repository assets, trust boundaries, attacker-controlled inputs, and security invariants.

Best for: first pass of a security audit or when the repository has unclear security assumptions.

### `finding-discovery`

Use after a threat model exists and you need to discover candidate security findings.

Best for: generating plausible security PR ideas, but do not treat candidates as valid until validation is done.

### `validation`

Use when there is already a candidate finding and you need evidence.

Best for: proving whether a suspected issue is real through tests, reproduction, code tracing, or realistic interface validation.

### `attack-path-analysis`

Use after a finding survives validation and you need to explain impact, exploit path, preconditions, and severity.

Best for: deciding whether a security finding is strong enough for a PR or report.

### `fix-finding`

Use when the user asks to fix a validated or plausible security finding.

Best for: implementing minimal security fixes with regression tests or repeatable validation.

## Superpowers Skills

### `brainstorming`

Use before creative or feature work. It clarifies the intended behavior before implementation.

Best for: new features, UX changes, or behavior additions where product intent matters.

### `test-driven-development`

Use before implementing features, bug fixes, refactors, or behavior changes.

Best for: PRs that can be protected with a focused test. Follow red, green, refactor whenever feasible.

### `systematic-debugging`

Use when something fails or behaves unexpectedly.

Best for: failing tests, broken app flows, runtime errors, CI failures, and reviewer-reported bugs. Do not patch before identifying root cause.

### `receiving-code-review`

Use when reviewer feedback appears on a PR.

Best for: understanding what the reviewer is actually asking, checking whether it is technically correct, and avoiding performative or blind changes.

### `writing-plans`

Use before large multi-step implementation work.

Best for: multi-file features, refactors, or work that should be broken into safe steps.

### `executing-plans`

Use when a written plan already exists and the agent must execute it.

Best for: following a pre-approved implementation plan in one session.

### `subagent-driven-development`

Use only when the environment supports subagents and the plan has independent tasks.

Best for: large implementations with disjoint work areas.

### `dispatching-parallel-agents`

Use only when multiple independent investigations can run in parallel.

Best for: independent failing tests, unrelated subsystems, or separate audit questions.

### `requesting-code-review`

Use when finishing major work and a second-pass review is useful.

Best for: catching regressions before pushing or handing a PR link to the user.

### `verification-before-completion`

Use before claiming that anything is done, fixed, verified, or passing.

Best for: final check discipline. Run fresh verification and report exact commands/results.

### `finishing-a-development-branch`

Use after implementation is complete and checks pass.

Best for: preparing final branch state, summary, and PR handoff.

### `using-git-worktrees`

Use when isolated workspaces are needed.

Best for: risky changes, multiple PR branches, or keeping current workspace clean.

### `using-superpowers`

Use as a general Superpowers entrypoint if the agent requires a global skill-loading rule.

Best for: agents that must establish how Superpowers should be invoked.

### `writing-skills`

Use when creating or editing skill documentation itself.

Best for: improving this kind of agent guidance.

## Practical PR Audit Flow

When the user asks for a deep audit for PR opportunities:

1. Inspect the repository structure and workflow first.
2. Run dependency install, tests, build, lint, or app workflow when feasible.
3. Check existing GitHub issues and PRs to avoid duplicates using the user's `gh` CLI from PowerShell.
4. For security/fix ideas, start with `<repo-root>\codex-security\63976030\skills\security-scan\SKILL.md`.
5. For feature ideas, start with `<repo-root>\superpowers\63976030\skills\brainstorming\SKILL.md`, then use `test-driven-development` when implementing.
6. For bug fixes, use `systematic-debugging` first, then `test-driven-development`.
7. For reviewer feedback, use `receiving-code-review`.
8. Before saying complete, use `verification-before-completion`.
9. Before handoff, follow `AGENTS_PR_RULE.md` and provide a ready-to-click PR creation link, not an auto-created PR.

Deep audit output MUST include both:

1. `Security / Fix PR Candidates`
2. `Feature PR Candidates`

Do not output only security findings. If one category has no safe PR candidate, include the category anyway and explain what was checked.

## Artifact And Report Location

When a user points to a repository, that repository folder is the target repo.

Any Markdown report, security artifact, Superpowers plan, scan note, or generated workflow output MUST be created inside the target repo folder only.

Do not create reports in:

- `D:\Portfolio Data\Learning PR`
- the parent workspace folder that contains multiple cloned repos
- unrelated cloned repositories
- plugin folders
- global agent workspaces
- directories outside the target repo

If a skill references a default artifact path outside the target repo, override it and choose a path inside the target repo, for example:

```text
<target-repo>\docs\agent-reports\
<target-repo>\docs\security\
<target-repo>\docs\superpowers\
<target-repo>\.agent\
```

Before writing any `.md` artifact, verify the resolved destination path starts with the target repo path.

## Important Guardrails

- Do not read every skill folder blindly. Choose based on the task.
- Do not use generic web search as the first method for GitHub open PR/issue discovery. Use `gh issue list` and `gh pr list` first.
- Do not propose PRs from static code guesses only. Run the repository workflow when feasible.
- Do not create PRs automatically. Push branches and provide ready-to-click PR creation links.
- Do not keep polling CI forever after a post-PR fix. Push the fix, report what changed, then stop.
- If a skill conflicts with direct user instructions, follow the user's explicit instructions first.
