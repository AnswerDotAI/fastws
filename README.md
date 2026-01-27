# fastws

Fast workspace tools for multi-repo management.

## Install

```bash
pip install fastws
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

### `ws_clone`

Clone all repos from your repos file:

```bash
ws_clone
ws_clone --repos-file myrepos.txt
ws_clone --workers 8
```

### `ws_pull`

Pull updates for all repos (parallel):

```bash
ws_pull
```

### `ws_status`

Show uncommitted changes and unpushed commits:

```bash
ws_status
```

### `ws_branches`

Check if all repos are on the expected branch:

```bash
ws_branches
ws_branches --expected develop
```
