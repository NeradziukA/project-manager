---
name: commit
description: Create a commit only after documentation and diagrams are updated
---

# Commit With Documentation Update

This skill ensures that every code commit includes updated documentation and logic diagrams.
Do not add a co-author section.

## Rules

Before creating a commit, you MUST perform the following steps:

1.  Pull latest changes before committing
    - Run `git pull` to avoid conflicts
    - If pull fails, stop and report the error
2.  Analyze the completed task
    - Determine what functionality was added, changed, or removed.
    - Identify affected modules, APIs, or logic flows.
3.  Update documentation
    - Update relevant files in:
      - README.md
    - Add or modify explanations for:
      - new bot commands
      - new environment variables
      - changed worker/bot behaviour
      - configuration changes
4.  Update logic diagrams
    - If the change affects the task flow or deploy flow, update the sequence diagram in README.md
    - Prefer mermaid format
5.  Verify documentation completeness
    - Ensure that new features are documented
    - Ensure outdated descriptions are removed or corrected
6.  Generate commit message based on the completed task
7.  Push to origin
    - After committing, push to:
      - `git push origin main`

Commit message format:

`<type>`: `<short description>`
Task: `<description of the implemented task>`
Allowed commit types: feat fix refactor docs perf test chore

## Bug Fix Rule

When a bug is fixed:

1. Write a regression test that fails without the fix and passes with it
2. The test must be committed together with the fix in the same commit
3. NEVER commit a bug fix without a covering test

## Important Constraints

NEVER create a commit if:

- diagrams are inconsistent with code
- the task description is missing
- a bug fix has no regression test
