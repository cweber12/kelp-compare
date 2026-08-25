# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues in `cweber12/kelp-compare`.
Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Project note

The repo is public. Don't put unpublished results, site coordinates not already
in `data/registry/sites.json`, or anything embargoed into an issue body — or
into a PR body, a commit message, or any file on a pushed branch. Branches are
pushed without a confirmation step (CLAUDE.md, "Branching and finishing a
task"), so nothing reviews this before it is public.
