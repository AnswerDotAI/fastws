# fastws

Fast workspace tools for multi-repo management.

## Install

```bash
pip install fastws-cli
```

## Setup

First, install uv if you haven't already, using the official script:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

If you've already installed uv, you might want to ensure it's up to date:

```bash
uv self update
```

Now you're ready to set up the workspace. First create a `repos.txt` file listing your repos (one per line) inside your desired workspace location. The script uses `~/aai-ws`.

```
AnswerDotAI/dialoghelper
AnswerDotAI/exhash
AnswerDotAI/fastgit
AnswerDotAI/fastship
AnswerDotAI/pyskills
AnswerDotAI/safepyrun
AnswerDotAI/shell_sage
AnswerDotAI/fastws
```

A line may add a checkout location after the repo, for repos that live outside the workspace root (`~` ok). Every command then reads and writes them there, and `ws-sync` editable-installs each Python project found inside (the repo root if it has a `pyproject.toml`, else its immediate subdirectories), so one out-of-tree repo can hold several small packages alongside non-Python content:

```
AnswerDotAI/fastcore
jph00/private ~/private
```

Then create and activate the uv environment (the examples use `~/aai-ws` as the workspace root; any directory works):

```bash
cd ~/aai-ws
uv venv --python 3.13 && source .venv/bin/activate
uv pip install fastws-cli
ws-clone
ws-sync
```

You will probably want to have the env auto-activated in all your shells, so run (modifying the location and shell rc file name as needed):

```bash
echo source ~/aai-ws/.venv/bin/activate >> ~/.bashrc
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

Pull updates in parallel, first asking GitHub (one batched GraphQL call) which repos' origins have actually moved, so unchanged repos are skipped silently. Repos that can't be checked (no `GITHUB_TOKEN`, detached HEAD, non-GitHub remote) are pulled the old way:

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

### `ws-build`

Build an sdist of each workspace project (including `repos.txt` checkouts outside the root) into a dists directory, `.dists` by default. A project is rebuilt only when a file in it is newer than its existing sdist, and older versions of a rebuilt package are pruned, so the directory always holds one current sdist per project. A failing project gets a warning without stopping the others, but the command then exits 1.

Progress goes to stderr; on success the dists path prints to stdout, so `$(ws-build)` in a script both refreshes the pool and yields its location. The result suits any resolver that takes a candidate pool, e.g. `uv pip install --find-links` or a docker build context for images that must test unreleased workspace packages:

```bash
ws-build
ws-build --force  # rebuild everything
ws-build --out /tmp/dists
```

### `ws-sync`

Sync the workspace metadata, pull local repos, and install updates. Like `ws-pull`, only repos whose GitHub origin has moved are pulled, so a typical sync is quiet and fast. By default it uses the active venv parent as the workspace root, so you do not need to `cd` first. If the workspace has no `pyproject.toml` yet, it copies `pyproject.tmpl` when present, else generates a minimal one, so a fresh directory syncs without setup. Any git checkout at the root that isn't in `repos.txt` yet is added to it, whether or not it's a Python project; `_`-prefixed directories are private and left alone. It respects `tool.uv.workspace.members` and `exclude` when scanning local projects, and if any member directory isn't a Python project yet (no `pyproject.toml`, e.g. a fresh empty clone), it warns and skips the `uv sync` step instead of letting uv fail on the whole workspace.

The workspace `exclude` list is auto-managed: a top-level directory that isn't a valid Python project gets excluded automatically, and is un-excluded once it gains a real `pyproject.toml`. A `repos.txt` checkout is auto-excluded only when it's a Cargo-only Rust crate; one with neither `pyproject.toml` nor `Cargo.toml` is treated as a pending member awaiting scaffolding and triggers the warning above instead. Globs, entries for missing directories, and entries for checkouts that are still not valid Python projects are kept, and `exclude = [...]` under `[tool.fastws]` in the workspace `pyproject.toml` declares intent the scan can't infer (e.g. keeping a real project out of the workspace). Hand-written `[tool.uv.sources]` entries (path, git, ...) are preserved when syncing adds new members.

Each sync also regenerates the `[patch]` entries in the workspace's `.cargo/config.toml`: one `[patch.crates-io]` entry per local crate (nested cargo workspace members included), plus an entry under the matching URL for each member git dep that names a local crate. This is the cargo analog of editable installs, so every build under the workspace root uses the local checkouts. Entries pointing outside the root, and all other config sections, are left alone. Do not commit a `Cargo.lock` generated under these patches: it records source-less local entries that no other machine can resolve.

When `sccache` is installed, sync also configures it as Cargo's `rustc-wrapper`, allowing unchanged compilation units to be reused across the workspace's otherwise-independent Cargo target directories. An existing wrapper is always preserved.

JavaScript packages join the same sync. A `package.json` in a workspace repo dir, or in an immediate subdirectory of one (`felt/package.json`, `mdhtml/wasm/package.json`), makes that dir a JS member. `node_modules`, `pkg`, and `_`-prefixed dirs are skipped. Each sync regenerates the `workspaces` list in the root `package.json`, creating the file the first time a member exists and keeping entries that point outside the root or use globs. After `uv sync` it runs the package manager's `install` at the root, so every member resolves its siblings through the root `node_modules` symlinks: the npm analog of editable installs. A member with a `Cargo.toml` beside its `package.json` is a native package (a wasm build). Sync runs its `build` script when `pkg/` is missing or older than any source in the member's repo, the parent crate included, which is the JS analog of `maturin develop`. The package manager is `npm` unless `[tool.fastws]` in the workspace `pyproject.toml` sets `js = "bun"` or `js = "pnpm"`. All three read the same `workspaces` field. A checkout that is only a JS package is a valid member: it is excluded from the uv workspace like a Cargo-only crate and is never treated as a pending scaffold.

At most once per day (tracked by a stamp file inside the workspace's `.git`, so git never sees it), the sync also floats dependencies: `uv sync -U` instead of plain `uv sync`, plus a parallel `cargo update` in every member with a `Cargo.toml`, printing what moved. Pass `--upgrade` to force that pass regardless of when it last ran.

Before every uv sync, `ws-sync` writes `.git/fastws-cargo-key` for each member crate. The key hashes `Cargo.lock` contents and the workspace Cargo patch configuration. For Git dependencies redirected to local paths by `[patch."<url>"]`, it also hashes each patched crate's `Cargo.toml`, `build.rs`, and `src` tree, recursively. The file is rewritten only when that content changes, so projects can use `{ file = ".git/fastws-cargo-key" }` in `tool.uv.cache-keys` without rebuilding after a timestamp-only `Cargo.lock` write.

```bash
ws-sync
ws-sync --workspace ~/aai-ws
ws-sync --upgrade
```

### `ws-add`

Add a repo to `repos.txt`, then run `ws-sync`. Given `owner/repo`, it clones; given the name of an existing local folder (e.g. one just scaffolded with `nbdev-new` or `ship-new`), it resolves `owner/repo` from the folder's `origin` remote instead, telling you exactly what's missing if the folder has no git repo, no GitHub origin, or no `pyproject.toml`. Given a path outside the workspace root, the repo stays where it is and its location is recorded in `repos.txt` (no root `pyproject.toml` needed: its packages are discovered on sync):

```bash
ws-add AnswerDotAI/fastws
ws-add answerdotai/fastws
ws-add fastws  # existing local folder, resolved via its origin remote
ws-add ~/private  # a path outside the workspace: stays where it is, recorded in repos.txt with its location
```

### `ws-remove`

Remove a repo: delete its clone, and drop it from `repos.txt` and the workspace `pyproject.toml`, then run `uv sync`. It refuses if the directory has uncommitted changes, unpushed commits, no `origin` remote, or isn't a clean git checkout, and always prompts for confirmation before deleting anything:

```bash
ws-remove AnswerDotAI/fastws
ws-remove fastws  # bare folder name also works if the directory exists
```

### `ws-releases`

Report repos with commits since their newest GitHub release, so nothing reviewed sits unshipped:

```bash
ws-releases                 # at the workspace root: sweep every repo in repos.txt
ws-releases solveit         # only solveit's transitive workspace dependencies
ws-releases --nodeps        # inside a repo: just that repo
ws-releases --skip 'wip'    # extra start-of-message regex for commits that need no release
```

Run from inside a workspace checkout, the sweep narrows to that repo and its transitive workspace dependencies; `--nodeps` narrows it to the repo alone (an error when no project is in play).

Each pending repo lists its unreleased commit summaries; repos with no releases yet get one quiet `no releases:` line, and fully-released repos appear in `up to date:`. The newest release is picked by version number (publish timestamps can be out of order), and repos whose default branch isn't `main` are handled automatically.

Commits whose message matches a start-anchored regex from the skip set need no release and aren't reported. The built-in set covers version bumps and housekeeping (`bump`, `nbdev regen`, `.gitignore`, `docs`, `CI`, ...: `DEFAULT_SKIP` in `fastws.releases`); `[tool.fastws]` in the workspace root `pyproject.toml` adds to it, and names repos that should never be swept (apps that deploy rather than release):

```toml
[tool.fastws]
release_skip = ["docs only"]
release_exclude = ["solveit", "md_site"]
```

From Python (the primary interface - the CLI is a thin wrapper over `fastws.releases`):

```python
from fastws import check_releases, check_release
await check_releases()            # ReleaseReport: the repr is the report
await check_releases('solveit')   # dependency-closure mode
await check_release('mdhtml')     # one repo: list of unreleased commit summaries (None = no releases)
```
