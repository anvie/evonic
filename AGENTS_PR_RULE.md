# AGENTS PR RULE

Rules for coding agents when the user asks for repository contribution work, PR ideas, issue analysis, fixes, features, refactors, or related GitHub tasks.

## User Context

- GitHub user: https://github.com/DeryFerd
- Default local workspace root: `D:\Portfolio Data\Learning PR`
- For active repo work, this rule file, `SKILLS.md`, and the folders `codex-security`, `superpowers`, and `humanizer` are expected to be copied into the target repository root.
- Agents MUST treat those repo-local files/folders as the source of truth. Do not depend on global skill paths outside the active repository workspace.
- The user is Indonesian and usually contributes to non-Indonesian open-source projects.
- Default PR title style: Conventional Commits.
- Default PR description language: English, friendly, natural, and copy-paste ready in Markdown.

## 1. When The User Gives A Repository Link

When the user gives a GitHub repository link, the agent MUST immediately:

1. Clone the repository into `D:\Portfolio Data\Learning PR` as a normal folder.
2. Prepare a ready-to-click fork link for the user.
3. STOP all further work.
4. Tell the user the local folder path and provide the ready-to-click fork link.

The agent MUST NOT start auditing, editing, scanning, or creating branches yet.

The user will trigger the next phase with one of these words:

- `scan`
- `deep audit`
- `deep analysis`

Only after one of those trigger words appears may the agent continue to repository analysis.

## 2. Deep Audit / Scan Workflow

When the user says `scan`, `deep audit`, or `deep analysis`, the user wants a deep repository audit for potential PRs and issues.

The agent MUST:

1. Inspect the codebase carefully.
2. Map relationships between major components.
3. Understand workflows like an experienced software engineer, not as a shallow grep-only scanner.
4. Check existing open PRs and issues in the target repository before proposing new PRs.
5. Avoid proposing PRs that duplicate or conflict with existing issues/PRs.

## 2.1 GitHub PR/Issue Lookup Source Of Truth

When checking existing PRs and issues, the agent MUST use the user's GitHub CLI (`gh`) from PowerShell as the primary source of truth.

The agent MUST NOT use generic web search, search engines, or browser scraping as the first method for open PR/issue discovery.

Use commands like:

```powershell
gh issue list --repo <owner>/<repo> --state open --limit 100
gh pr list --repo <owner>/<repo> --state open --limit 100
gh issue view <number> --repo <owner>/<repo>
gh pr view <number> --repo <owner>/<repo>
```

If more precision is needed, use JSON output:

```powershell
gh issue list --repo <owner>/<repo> --state open --limit 100 --json number,title,labels,author,url,updatedAt
gh pr list --repo <owner>/<repo> --state open --limit 100 --json number,title,headRefName,author,url,updatedAt
```

Web access is only allowed as a fallback when `gh` is unavailable, unauthenticated, or cannot access the repository. If using fallback web access, the agent MUST explicitly tell the user why `gh` could not be used.

Before proposing PR ideas, the agent MUST summarize that open PRs/issues were checked via `gh` and mention any overlap risks found.

## 3. Required Skills

After initial codebase scan, the agent MUST use the repository/workspace skill router first:

```text
<target-repo>\SKILLS.md
```

This file explains which skill subfolder should be used for each task type.

If the current IDE or coding agent explicitly requires a `SKILLS.md` entrypoint, use this router file as the required entrypoint and then open only the relevant subfolder's `SKILL.md`.

The target repo is expected to contain these local folders and files:

```text
<target-repo>\AGENTS_PR_RULE.md
<target-repo>\AGENTS_POST_PR_RULE.md
<target-repo>\SKILLS.md
<target-repo>\codex-security\
<target-repo>\superpowers\
<target-repo>\humanizer\
```

These local rules and skill folders are NON-NEGOTIABLE. The agent MUST obey them before using its own assumptions or external/global skill locations.

Before deep audit, implementation, post-PR work, or reviewer-comment work, the agent MUST verify these repo-local files/folders exist.

If any of them are missing, the agent MUST STOP immediately and ask the user:

```text
Apakah file dan folder ada?
```

The agent MUST NOT continue with global paths, web search, inferred skill behavior, or partial skill access when these repo-local rules/skill folders are missing.

The underlying skill folders are:

```text
<target-repo>\codex-security\63976030\skills
```

```text
<target-repo>\superpowers\63976030\skills
```

```text
<target-repo>\humanizer
```

Use them as follows:

- Security, vulnerability, auth, data exposure, and similar work: start with `codex-security/security-scan/SKILL.md`, then use phase skills only when appropriate.
- Fixing a validated security issue: use `codex-security/fix-finding/SKILL.md`.
- Feature requests and behavior changes: use `superpowers/brainstorming/SKILL.md` first when product intent is unclear, then `superpowers/test-driven-development/SKILL.md`.
- Bug fixes and broken workflows: use `superpowers/systematic-debugging/SKILL.md`, then `superpowers/test-driven-development/SKILL.md` when writing the fix.
- Reviewer feedback: use `superpowers/receiving-code-review/SKILL.md`.
- Completion claims: use `superpowers/verification-before-completion/SKILL.md`.
- Performance, refactor, cleanup, DX, and other non-security/non-feature tasks: use natural coding ability and optionally use the above skills when helpful.

During a deep audit, the agent MUST NOT only produce security findings.

The audit output MUST include both:

1. `Security / Fix PR Candidates`
2. `Feature PR Candidates`

If no strong candidate exists in one category, the agent MUST still include that category and explain what was checked and why no safe PR candidate was proposed.

For `Security / Fix PR Candidates`, use the local `codex-security` skills.

For `Feature PR Candidates`, use the local `superpowers` skills, especially `brainstorming`, `test-driven-development`, and `verification-before-completion`.

For human-friendly PR descriptions, also use:

```text
<target-repo>\humanizer
```

## 3.1 Skill Report / Artifact Location

When the user points to a specific repository, that repository folder becomes the target repo.

All reports, scan artifacts, plans, temporary Markdown outputs, or notes created by `codex-security`, `superpowers`, or any related workflow MUST be written inside the target repo folder.

The agent MUST NOT write skill reports or generated `.md` files into:

- `D:\Portfolio Data\Learning PR`
- the parent workspace folder that contains multiple cloned repos
- another cloned repository
- the skill plugin folders
- the agent's global workspace
- any directory outside the target repo

Recommended locations inside the target repo:

```text
<target-repo>\docs\agent-reports\
<target-repo>\docs\security\
<target-repo>\docs\superpowers\
<target-repo>\.agent\
```

If the skill suggests a default artifact path outside the target repo, override it and place the artifact inside the target repo instead.

If the report is only meant for the user in chat, prefer writing it directly in chat and do not create an extra file.

Before creating any `.md` report or artifact, the agent MUST confirm its resolved path is under the target repo directory.

## 4. Non-Negotiable: Run The Workflow

The agent MUST NOT rely only on code assumptions.

During deep audit and validation, the agent MUST run the repository workflow whenever feasible:

- Install dependencies if needed.
- Run available checks/tests/lints/builds.
- Run the app or relevant workflow when the potential PR affects runtime behavior or UX.
- Manually exercise the affected flow when static checks are insufficient.

Good PRs come from finding or validating real behavior in the running project, not from code-only assumptions.

If the app cannot be run, the agent MUST explain exactly why and what was still verified.

## 5. Protect Existing Workflows

Potential PRs MUST NOT break existing workflows.

Before proposing or implementing a PR, the agent MUST think through:

- Existing user flows.
- Setup/onboarding flows.
- Admin/member/no-auth differences.
- Backward compatibility.
- UI/UX regressions.
- CI/checks likely used by the repository.

## 6. Report Findings To The User

When presenting potential PRs, the agent MUST include:

- Clear title idea.
- What problem it addresses.
- Why it matters.
- Main affected files/components.
- Risk level.
- Whether it is `Code-only` or `Must Run App`.

The findings report MUST be grouped into these sections:

```markdown
## Security / Fix PR Candidates
```

```markdown
## Feature PR Candidates
```

Optional additional categories may be added after those two, for example:

```markdown
## Refactor / Performance / DX Candidates
```

But the agent MUST NOT omit the security/fix and feature sections.

Definitions:

- `Code-only`: can be implemented and reasonably verified by code checks/tests without manual app workflow.
- `Must Run App`: requires running the app or manual workflow validation because it affects UX, auth, setup, browser, UI, project creation, tunnel flows, file picker, etc.

## 7. If User Selects Must Run App

If the user chooses a `Must Run App` PR, the agent MUST guide the user step-by-step:

1. How to install dependencies.
2. How to start the app.
3. Which account/mode/role to use.
4. What exact UI/workflow to click.
5. What expected result confirms the finding.
6. What screenshots or notes are useful.

Do this before implementing or finalizing the PR when manual validation is needed.

## 8. If User Selects Code-only

If the user chooses a `Code-only` PR, the agent should implement directly.

Still run available checks/tests before pushing.

## 9. Mixed Requests

The user may choose both `Must Run App` and `Code-only` PRs in one request.

In that case:

1. Handle `Must Run App` validation first.
2. Finish that flow.
3. Then implement `Code-only` work.

## 10. Implementing And Pushing

After implementing a selected PR:

1. Create a dedicated branch from the correct base branch.
2. Commit only relevant files.
3. Push the branch to the user's fork.
4. Do not create the PR automatically.

Use the user's fork when available:

```text
https://github.com/DeryFerd/<repo>
```

## 11. Ready-To-Click PR Links

The user wants to create PRs manually.

The agent MUST NOT create PRs automatically.

The agent MUST provide a ready-to-click PR creation link that opens GitHub's PR creation page with the title/body box visible.

Preferred link formats:

```text
https://github.com/<owner>/<repo>/compare/<base-branch>...DeryFerd:<branch-name>?expand=1
```

or, when GitHub provides it after push:

```text
https://github.com/DeryFerd/<repo>/pull/new/<branch-name>
```

The agent MUST NOT provide only a generic compare link without `?expand=1` if that link does not open the PR creation form.

If unsure, prefer:

```text
https://github.com/<owner>/<repo>/compare/main...DeryFerd:<branch-name>?expand=1
```

Adjust `main` if the repository's base branch is different.

## 12. PR Title Rule

PR titles MUST use Conventional Commits style.

Examples:

```text
fix(auth): enforce project access for session routes
feat(files): add project-scoped path resolver
refactor(db): centralize connection ownership checks
test(tunnel): cover admin-only route access
```

## 13. PR Description Trigger

By default, the agent may leave PR descriptions empty unless the user asks.

When the user says one of these triggers:

- `deskripsi`
- `desc`
- `description`

the agent MUST generate a PR description.

## 14. PR Description Rules

When generating descriptions:

- Write directly in chat.
- Use Markdown.
- Do not create a file.
- Prefer English unless the user asks for Indonesian.
- Keep it friendly and natural, not robotic.
- Make it detailed enough for maintainers/reviewers to understand the PR without guessing.
- Do not give a tiny two-sentence description unless the PR is truly trivial.
- Prefer a substantial description that explains context, motivation, what changed, validation, and any relevant risk or compatibility notes.
- Avoid obvious AI-generated phrasing.
- Use the humanizer skills at:

```text
<target-repo>\humanizer
```

If the user asks for descriptions for multiple PRs, do NOT reuse the same template for every PR.

Bad repeated template for every PR:

```text
Problem
Why?
What Changed
Test Run
```

Instead, vary structure and tone per PR while keeping it clear and copy-paste friendly.

## 15. After PR Links / Descriptions

After providing pushed branches, ready-to-click PR links, titles, or descriptions:

1. STOP.
2. Wait for the user to create/process the PRs manually.
3. Do not continue into post-PR checks unless the user asks.

## 16. Post-PR Workflow

These rules apply after the user has created a pull request and asks about PR status, reviewer comments, failing checks, CI, or follow-up fixes.

This section is intentionally included in this main PR rule file so agents do not miss it when the separate post-PR file is not provided.

## 17. Post-PR CI / Checks

After the user creates a PR, the agent should help the user check whether the PR is safe.

Ask or clarify:

- Is the PR okay without CI?
- Are there GitHub Actions or required checks?
- Do any checks fail?
- Does the user want the agent to inspect failing checks?

When the user provides a PR link, the agent MUST analyze it using GitHub CLI (`gh`) from PowerShell when possible.

The user has `gh` installed in PowerShell.

Use commands like:

```powershell
gh pr view <number> --repo <owner>/<repo> --json ...
gh pr checks <number> --repo <owner>/<repo>
gh run view <run-id> --repo <owner>/<repo> --log-failed
```

If CI/checks are failing, the agent MUST:

1. Identify the failed check/job.
2. Read the relevant failed logs.
3. Determine the root cause.
4. Patch the PR branch if needed.
5. Run the closest local check/test when feasible.
6. Push a fix commit to the same PR branch.

Do not guess from the check name only. Read logs.

## 18. Post-PR: Do Not Loop Waiting For CI

The agent MUST NOT keep polling GitHub Actions until CI finishes.

After pushing a fix:

1. Tell the user what was fixed.
2. Tell the user which check/log caused the fix.
3. Tell the user that CI has been triggered again.
4. STOP.
5. Let the user wait for GitHub Actions/reviewer.

Acceptable short checks:

- One immediate `gh pr checks` snapshot.
- One `gh run view --log-failed` for failed jobs.
- One local verification command.

Not acceptable:

- Repeated polling until all checks pass.
- Waiting many minutes in a loop.
- Running forever because checks are pending.

## 19. Post-PR Reviewer Comments

When the user asks to address reviewer comments:

1. Read PR comments, review comments, and issue comments.
2. Understand each requested change.
3. Reproduce or validate the issue when feasible.
4. Fix only the relevant PR branch.
5. Run checks and manual workflow if the comment mentions UX/runtime behavior.
6. Push a follow-up commit to the same PR branch.
7. Do not post a GitHub comment unless the user asks or explicitly approves.

The agent MUST NOT change the main PR title.

The agent MUST NOT change the main PR description/body.

The agent MUST NOT rewrite, shorten, expand, reformat, or "improve" the main PR title or description while addressing reviewer feedback.

If communication is needed, the agent should comment/reply to the reviewer or provide text for the user to paste, depending on what the user asked.

Only edit the PR title or main PR description if the user explicitly asks for that exact change.

## 20. Post-PR Commenting Back To Reviewers

If the user asks the agent to comment back:

- Keep it concise, friendly, and factual.
- Mention checks/manual tests actually performed.
- Never mention secrets, local tokens, or sensitive data.
- Do not overclaim.
- Do not edit the main PR title or description as a substitute for replying.

Example:

```markdown
Thanks for the review. I pushed a follow-up commit that addresses the regression.

Validation:
- `bun run check` passes.
- `bun run lint` passes.
- Manually verified the affected flow in the running app.
```

## 21. Post-PR End State

After fixing a PR or replying to reviewer comments:

1. Provide a short summary to the user.
2. Include commit hash and PR link when useful.
3. Mention what was verified.
4. STOP.
5. Wait for the user or reviewer response.
