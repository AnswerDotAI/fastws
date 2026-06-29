# fastws

Fast workspace tools for multi-repo management.

## Install

```bash
pip install fastws-cli
```

## Setup

Create a `repos.txt` file listing your repos (one per line):

```
AnswerDotAI/fastcore
AnswerDotAI/fastgit
AnswerDotAI/fastship
AnswerDotAI/fastws
```

## Commands

### `ws-clone`

Clone all repos from your repos file:

```bash
ws-clone
ws-clone --repos-file myrepos.txt
ws-clone --workers 8
```

### `ws-pull`

Pull updates for all repos (parallel):

```bash
ws-pull
```

### `ws-status`

Show uncommitted changes and unpushed commits:

```bash
ws-status
ws-status --branches
```

### `ws-branches`

Check if all repos are on the expected branch:

```bash
ws-branches
ws-branches --expected develop
```

### `ws-sync`

Sync the workspace metadata, pull local repos, and install updates.
By default it uses the active venv parent as the workspace root, so you do not need to `cd` first:
It respects `tool.uv.workspace.members` and `exclude` when scanning local projects.

```bash
ws-sync
ws-sync --workspace ~/aai-ws
```

### `ws-add`

Add a repo to `repos.txt`, then run `ws-sync`:

```bash
ws-add AnswerDotAI/fastws
ws-add answerdotai/fastws
```

### `ws-remove`

Remove a repo: delete its clone, and drop it from `repos.txt` and the workspace
`pyproject.toml`, then run `uv sync`. It refuses if the directory has uncommitted
changes, unpushed commits, no `origin` remote, or isn't a clean git checkout, and
always prompts for confirmation before deleting anything:

```bash
ws-remove AnswerDotAI/fastws
```
