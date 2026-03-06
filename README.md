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
```

### `ws-branches`

Check if all repos are on the expected branch:

```bash
ws-branches
ws-branches --expected develop
```
