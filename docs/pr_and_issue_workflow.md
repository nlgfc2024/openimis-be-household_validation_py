# Pull Request and Issue Workflow Notes

These notes describe the current workflow for the household validation backend draft PR and the implementation issues created in this repository.

## Current PR

- PR: `#4` Build household validation backend foundation
- Head branch: `feature/household-validation`
- Base branch: `sprint/2026-07`
- Current state: draft
- Current requested reviewers: `Shahzaibahmad97`, `mcleanka`, `uniquedj95`, `weilu`

## Reviewer Requests

GitHub reviewer requests are requests for review, not a per-user mandatory approval rule.

Adding multiple reviewers is useful when the team wants optional input from more people. A reviewer becomes mandatory only if repository rules require that person, a CODEOWNERS rule applies, or branch protection/rulesets require a certain number of approvals before merge.

For this PR, the extra reviewers should be treated as optional unless the repository's branch rules or maintainers say otherwise. If only one approval is required by the branch rules, any eligible approval may satisfy that requirement.

## PR States

- Draft: The PR is open but not ready for formal review or merge. Draft PRs cannot be merged.
- Ready for review: The PR is active and reviewable. Checks and required reviews can be used to decide whether it can merge.
- Closed: The PR was closed without merging.
- Merged: The PR was merged into the base branch.

Use draft while the backend work is still being grouped into sub-PR sections or while follow-up implementation issues are still being added. Mark the PR ready for review only after the intended backend implementation scope is complete, validation has passed, and the PR description accurately lists the included sub-PR sections.

## Review States

- Review requested: A user or team has been asked to review.
- Commented: A reviewer left comments without approving or requesting changes.
- Approved: A reviewer approved the PR.
- Changes requested: A reviewer requested changes. Treat this as blocking until resolved if branch rules require review approval.

## Implementation Issues

The backend work has been split into implementation issues and documented as sub-PR sections inside PR `#4`.

| Issue | State | PR section | Scope |
| --- | --- | --- | --- |
| `#1` | Closed | Sub PR 1 | Module config and permissions |
| `#3` | Open | Sub PR 2 | Validation batch tracking models |
| `#5` | Open | Sub PR 3 | Eligible household selection service |
| `#6` | Open | Sub PR 4 | Excel validation list export |
| `#7` | Open | Sub PR 5 | Project lookup |
| `#8` | Open | Sub PR 6 | Excel upload and apply workflow |
| `#9` | Open | Sub PR 7 | GraphQL API surface |
| `#10` | Open | Sub PR 8 | Backend test coverage |
| `#11` | Open | Sub PR 9 | District validation role assignments |

Issue `#4` is the pull request itself, not a separate implementation issue.

## How to Link Issues

Use closing keywords in the relevant PR section when an implementation issue is completed:

```text
Issue: Closes #11
```

Supported keywords include `Closes`, `Fixes`, and `Resolves`. Keep one issue reference per sub-PR section so reviewers can map code, commit, and issue scope cleanly.

## When to Close Issues

Do not manually close implementation issues just because the code was committed to the feature branch. Keep the issue open while the PR is still draft, under review, or waiting to merge.

Close an implementation issue when one of these is true:

- The PR containing the completed work has merged into the repository's default branch and GitHub closes the issue automatically.
- The PR has merged into a non-default sprint branch and maintainers decide the work is accepted for that sprint; close the issue manually with a comment referencing PR `#4` and the merge commit.
- The issue is intentionally cancelled, superseded, or moved to another issue; close it manually with a short explanation and a link to the replacement issue or PR.

Because PR `#4` currently targets `sprint/2026-07`, do not rely on `Closes #N` to auto-close issues unless `sprint/2026-07` is the repository default branch. GitHub's closing keywords are interpreted for automatic issue closure when the PR targets the default branch.

## Ready-for-Review Checklist

Before changing PR `#4` from draft to ready for review:

- Confirm no planned backend implementation issue remains in scope for this PR.
- Confirm issue `#9` from the implementation plan remains intentionally excluded because frontend work is handled separately.
- Confirm the PR body includes the latest sub-PR section and commit list.
- Confirm tests and Django validation commands listed in the PR body still pass.
- Confirm requested reviewers are correct.

After the checklist is complete, mark the PR ready with:

```bash
gh pr ready 4
```

## References

- GitHub pull request docs: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/about-pull-requests
- GitHub pull request review docs: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/about-pull-request-reviews
- GitHub issue linking docs: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
