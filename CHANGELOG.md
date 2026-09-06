<!-- do not remove -->

## 0.0.13

### New Features

- Build a selected package and its workspace dependencies ([#31](https://github.com/AnswerDotAI/fastws/pull/31)), thanks to [@jph00](https://github.com/jph00)
- Cache Rust compilation across workspace projects ([#28](https://github.com/AnswerDotAI/fastws/pull/28)), thanks to [@jph00](https://github.com/jph00)
- Add --workers option to ws-add ([#27](https://github.com/AnswerDotAI/fastws/pull/27)), thanks to [@ncoop57](https://github.com/ncoop57)

### Bugs Squashed

- Ignore unused Cargo patches in build cache keys ([#29](https://github.com/AnswerDotAI/fastws/pull/29)), thanks to [@jph00](https://github.com/jph00)


## 0.0.12

### New Features

- Improve DevX for external users ([#10](https://github.com/AnswerDotAI/fastws/pull/10)), thanks to [@kafkasl](https://github.com/kafkasl)


## 0.0.11

### New Features

- fastws: auto-generate Cargo  entries in .cargo/config.toml and treat all root git checkouts as crates, not just uv members ([#26](https://github.com/AnswerDotAI/fastws/issues/26))


## 0.0.10

### New Features

- Make ws-pull/ws-sync async, skipping repos whose GitHub origin has not moved; switch to fastgit Git API ([#25](https://github.com/AnswerDotAI/fastws/issues/25))
- Filter out Updating ([#24](https://github.com/AnswerDotAI/fastws/issues/24))


## 0.0.9

### New Features

- Add ws-build: build sdists of all workspace projects into a dists dir, skipping unchanged projects and pruning old versions ([#23](https://github.com/AnswerDotAI/fastws/issues/23))
- detect project from cwd, add --nodeps to skip the dependency closure, and map dir names to packages ([#22](https://github.com/AnswerDotAI/fastws/issues/22))
- Move `dep_key`, member graph, and closure helpers to ghapi (>=2.0.6) and use `dep_key`/`local_dep_graph`/`dep_closure` in core and releases ([#21](https://github.com/AnswerDotAI/fastws/issues/21))
- ws-sync: write content-hashed .git/fastws-cargo-key per crate, covering Cargo.lock and locally patched git deps, for uv cache-keys ([#20](https://github.com/AnswerDotAI/fastws/issues/20))
- Run cargo update before uv sync in `ws_sync` so Rust deps refresh first ([#19](https://github.com/AnswerDotAI/fastws/issues/19))
- Add out-of-tree repo support, auto-managed workspace excludes, and uv.sources preservation to ws-sync ([#18](https://github.com/AnswerDotAI/fastws/issues/18))


## 0.0.8

### New Features

- Add ws-releases: report unreleased commits across the workspace, with configurable skip patterns and repo exclusions ([#17](https://github.com/AnswerDotAI/fastws/pull/17)), thanks to [@jph00](https://github.com/jph00)
- Add once-daily dependency floating to `ws_sync` (uv sync -U + cargo update), with --upgrade flag to force ([#15](https://github.com/AnswerDotAI/fastws/issues/15))
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
