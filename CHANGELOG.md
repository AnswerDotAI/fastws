<!-- do not remove -->

## 0.0.8

### New Features

- Add ws-releases: report unreleased commits across the workspace, with configurable skip patterns and repo exclusions ([#17](https://github.com/AnswerDotAI/fastws/pull/17)), thanks to [@jph00](https://github.com/jph00)
- Add once-daily dependency floating to ws_sync (uv sync -U + cargo update), with --upgrade flag to force ([#15](https://github.com/AnswerDotAI/fastws/issues/15))
- ws-sync skips uv sync for member dirs lacking pyproject.toml; ws-add resolves local folder names via origin remote ([#12](https://github.com/AnswerDotAI/fastws/issues/12))
- Allow bare folder name in ws-remove; separate directory deletion confirmation from metadata removal ([#9](https://github.com/AnswerDotAI/fastws/issues/9))
- Add `ws-remove` command to delete a repo from the workspace; refactor CLI wrappers with `@delegates` ([#8](https://github.com/AnswerDotAI/fastws/issues/8))
- Add [dev] extra to workspace dependencies and filter spurious warnings ([#7](https://github.com/AnswerDotAI/fastws/pull/7)), thanks to [@RensDimmendaal](https://github.com/RensDimmendaal)
- add --workers flag to ws-sync ([#6](https://github.com/AnswerDotAI/fastws/pull/6)), thanks to [@RensDimmendaal](https://github.com/RensDimmendaal)

### Bugs Squashed

- Preserving existing tool.uv.sources entries during ws-sync ([#14](https://github.com/AnswerDotAI/fastws/pull/14)), thanks to [@kafkasl](https://github.com/kafkasl)


## 0.0.7

### New Features

- Refactor Pyright editable path setup to write pyrightconfig.json instead of .pth files ([#4](https://github.com/AnswerDotAI/fastws/issues/4))


## 0.0.6

### New Features

- Add root-aware clone/pull helpers, auto-pull in ws-sync, and docstrings to CLI entry points ([#2](https://github.com/AnswerDotAI/fastws/issues/2))


## 0.0.5

### New Features

- Add ws-sync and ws-add commands with workspace metadata sync and Pyright editable path support ([#1](https://github.com/AnswerDotAI/fastws/issues/1))


## 0.0.3

- change names

## 0.0.2

- init release
