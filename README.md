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
By default it uses the active venv parent as the workspace root, so you do not need to `cd` first.
It respects `tool.uv.workspace.members` and `exclude` when scanning local projects, and if any
member directory isn't a Python project yet (no `pyproject.toml`, e.g. a fresh empty clone), it
warns and skips the `uv sync` step instead of letting uv fail on the whole workspace.

```bash
ws-sync
ws-sync --workspace ~/aai-ws
```

### `ws-add`

Add a repo to `repos.txt`, then run `ws-sync`. Given `owner/repo`, it clones; given the
name of an existing local folder (e.g. one just scaffolded with `nbdev-new` or `ship-new`),
it resolves `owner/repo` from the folder's `origin` remote instead, telling you exactly
what's missing if the folder has no git repo, no GitHub origin, or no `pyproject.toml`:

```bash
ws-add AnswerDotAI/fastws
ws-add answerdotai/fastws
ws-add fastws  # existing local folder, resolved via its origin remote
```

### `ws-remove`

Remove a repo: delete its clone, and drop it from `repos.txt` and the workspace
`pyproject.toml`, then run `uv sync`. It refuses if the directory has uncommitted
changes, unpushed commits, no `origin` remote, or isn't a clean git checkout, and
always prompts for confirmation before deleting anything:

```bash
ws-remove AnswerDotAI/fastws
ws-remove fastws  # bare folder name also works if the directory exists
```
