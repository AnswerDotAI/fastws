# fastws

fastws manages independent Git repositories as one development workspace. Python projects share a uv environment with editable installs, so changes in one checkout are available to the projects that depend on it. Local Rust crates receive Cargo dependency overrides for the same purpose.

The workspace keeps a shared team baseline in `repos.txt` and personal additions in `repos-local.txt`. Each project retains its own Git history, branches, and release process. fastws provides commands to clone and sync the workspace, inspect changes across repos, build local distributions, and find unreleased changes along dependency chains.

## Install

```bash
pip install fastws-cli
```

## Setup

With uv, Git, and GitHub SSH access configured, bootstrap the public Answer.AI workspace in a new directory:

```bash
uvx --from 'fastws-cli>=0.0.14' ws-setup AnswerDotAI/aai-ws ~/aai-ws
```

Substitute your team's workspace repo or your own fork. For the private Answer.AI workspace, use `AnswerDotAI/private-ws`. `ws-setup` creates the environment, installs fastws, syncs the workspace, and prints how to activate it. The public baseline also needs Rust and native build tools for its source checkouts. Continue with `aai-coding/SETUP.md` to configure the coding harness.

### Manual setup

The following steps explain the workspace layout and can be used to build a baseline of your own.

Install uv if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

To update an existing uv installation:

```bash
uv self update
```

Clone your team's workspace repo, or create a directory with a `repos.txt` listing the shared baseline, one repo per line. The examples use `~/aai-ws`, but any directory works.

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

For a repo outside the workspace root, put its checkout location after the name. Paths can use `~`. Every command uses that location. `ws-sync` editable-installs the repo root if it contains a `pyproject.toml`; otherwise it checks the immediate subdirectories for Python projects. An external repo can therefore contain several packages alongside non-Python content:

```
AnswerDotAI/fastcore
jph00/private ~/private
```

### Shared baseline and personal additions

Keep `repos.txt` tracked in the workspace Git repo. Put personal additions in a gitignored `repos-local.txt`, using the same format. Every command reads both lists. `ws-add` and automatic discovery append only to the local list. Edit the shared list deliberately when changing the team's baseline. Duplicate repo names are matched case-insensitively. Conflicting explicit locations, or two different repos targeting the same directory, cause errors.

`--repos-file myrepos.txt` uses `myrepos-local.txt` alongside it. Either list may be absent. Removing a repo from the baseline does not delete its checkout. The next sync discovers an existing root checkout as a personal addition. External checkouts also remain on disk; add them to the local list to keep managing them.

The workspace `pyproject.toml` is local generated state. `ws-sync` creates it from the tracked `pyproject.tmpl` when available, or generates a minimal one, then maintains it without replacing personal settings. The template supplies starting defaults only: later template edits do not overwrite existing configuration. Ignore these files in the workspace repo (not in its member repos):

```gitignore
/repos-local.txt
/pyproject.toml
/pyrightconfig.json
/uv.lock
/.cargo/
/.fastws-upgraded
```

Keep project checkouts out of the workspace Git index too. When migrating a workspace that already tracks generated files, save local copies before pulling the change that untracks them, then restore those copies afterward. Also save personal repo entries to `repos-local.txt` before pulling a smaller baseline.

Then create and activate the uv environment (the examples use `~/aai-ws` as the workspace root; any directory works):

```bash
cd ~/aai-ws
uv venv --python 3.13 && source .venv/bin/activate
uv pip install fastws-cli
ws-sync
```

To activate the environment automatically in new shells, add it to the appropriate shell startup file. Adjust the path and filename as needed:

```bash
echo source ~/aai-ws/.venv/bin/activate >> ~/.bashrc
```

## Commands

### `ws-setup`

Clone a workspace repo into a new directory, create its `.venv`, install fastws, and run that environment's `ws-sync`. It targets the new environment even when another workspace or uv tool environment is already active. It does not change the caller's environment, shell startup files, or harness settings.

```bash
ws-setup AnswerDotAI/aai-ws ~/aai-ws
ws-setup AnswerDotAI/private-ws ~/aai-ws
ws-setup owner/workspace ~/my-workspace --python 3.13
```

The destination must not exist, including as an empty directory or a symlink. A failed step stops setup and leaves the checkout in place for inspection; it never deletes a partial workspace automatically. For an existing workspace, activate its environment and use `ws-sync` instead. Workspace repos should include fastws in their repo list or package dependencies so syncing retains the workspace commands.

### `ws-clone`

Clone missing repos from both lists, without pulling the workspace repo or installing packages. A failed clone or an existing non-Git directory at a target causes an error:

```bash
ws-clone
ws-clone --repos-file myrepos.txt
ws-clone --workers 8
```

### `ws-pull`

Pull updates in parallel. A batched GraphQL request checks which GitHub origins have changed and skips unchanged repos. Repos that cannot be checked use a normal pull, including those without `GITHUB_TOKEN`, with a detached HEAD, or with a non-GitHub remote:

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

Build an sdist of each workspace project, including `repos.txt` checkouts outside the root, into `.dists` or a specified output directory. A project is rebuilt only when a file in it is newer than its existing sdist. Rebuilding removes older versions of that package from the output directory. A failed build produces a warning and does not stop the other builds. The command exits with status 1 if any project failed.

Progress goes to stderr. On success, the output directory path goes to stdout. Scripts can use `$(ws-build)` to build the packages and obtain that path. Use the distributions with a resolver such as `uv pip install --find-links`, or in a Docker build context that needs unreleased workspace packages:

```bash
ws-build
ws-build --force  # rebuild everything
ws-build --out /tmp/dists
ws-build --project solveit  # only this package and its transitive workspace dependencies
```

`--project` selects by package name and follows declared runtime and build dependencies, including workspace packages in external checkouts. Other packages in the output directory are left untouched. Without it, the whole workspace is built as before.

### `ws-sync`

Syncing proceeds in this order:

1. Pull the workspace repo if it has an upstream, using its normal Git pull configuration. A failed pull stops the sync. fastws does not stash or discard local changes.
2. Read the updated shared and local lists and clone missing repos. Clone failures stop the sync before installation.
3. Pull project updates, using the same changed-origin check as `ws-pull`.
4. Update workspace metadata and install the projects.

By default, `ws-sync` uses the active venv's parent as the workspace root. It creates a missing `pyproject.toml` as described above. GitHub checkouts at the root that are absent from both lists are added to `repos-local.txt`, including non-Python repos. Directories prefixed with `_` are left alone unless explicitly listed.

Project scanning respects `tool.uv.workspace.members` and `exclude`. If a member directory lacks `pyproject.toml`, such as a fresh empty clone, sync warns and skips the `uv sync` step.

The workspace `exclude` list is maintained automatically. An unlisted top-level directory that is not a valid Python project is excluded until it has a `pyproject.toml`. A `repos.txt` checkout is automatically excluded only when it is a Cargo-only Rust crate. A listed checkout with neither `pyproject.toml` nor `Cargo.toml` is treated as a pending member and triggers the warning above.

Existing globs, entries for missing directories, and entries for checkouts that are still not Python projects are retained. Use `exclude = [...]` under `[tool.fastws]` to specify exclusions the scan cannot infer, such as keeping a valid project out of the workspace. Adding members preserves hand-written `[tool.uv.sources]` entries, including path and Git sources.

Each sync regenerates Cargo overrides in the workspace's `.cargo/config.toml`. `[patch.crates-io]` gets an entry for each local crate, including nested Cargo workspace members. Git dependencies that name local crates get entries under the matching URL. Builds under the workspace root use these local checkouts. Entries pointing outside the root and other configuration sections are preserved.

Do not commit a `Cargo.lock` generated under these patches. Its source-less local entries cannot be resolved on another machine.

When `sccache` is installed, sync also configures it as Cargo's `rustc-wrapper`, allowing unchanged compilation units to be reused across the workspace's otherwise-independent Cargo target directories. An existing wrapper is always preserved.

At most once per day, sync upgrades dependencies with `uv sync -U` and a parallel `cargo update` in every member with a `Cargo.toml`. It prints the changes and records the run in a stamp file inside the workspace's `.git` directory. Pass `--upgrade` to force an upgrade regardless of the last run time.

Before every uv sync, `ws-sync` writes `.git/fastws-cargo-key` for each member crate. The key hashes `Cargo.lock` contents and the workspace Cargo patch configuration. For Git dependencies redirected to local paths by `[patch."<url>"]`, it also hashes each patched crate's `Cargo.toml`, `build.rs`, and `src` tree, recursively. The file is rewritten only when that content changes, so projects can use `{ file = ".git/fastws-cargo-key" }` in `tool.uv.cache-keys` without rebuilding after a timestamp-only `Cargo.lock` write.

```bash
ws-sync
ws-sync --workspace ~/aai-ws
ws-sync --upgrade
```

### `ws-add`

Add a repo to `repos-local.txt`, then run `ws-sync`. Repos already in either list are not added again.

The argument determines how fastws locates the checkout:

- `owner/repo` clones the repository.
- An existing local folder, such as one created by `nbdev-new` or `ship-new`, uses its `origin` remote to resolve `owner/repo`. The command reports a missing Git repo, GitHub origin, or `pyproject.toml`.
- A path outside the workspace root leaves the checkout in place and records its location in the local list. The checkout needs no root `pyproject.toml`. Sync discovers its packages.

```bash
ws-add AnswerDotAI/fastws
ws-add answerdotai/fastws
ws-add fastws  # existing local folder, resolved via its origin remote
ws-add ~/private  # stays where it is, recorded in repos-local.txt with its location
```

### `ws-remove`

Delete a personal repo's clone, remove it from `repos-local.txt` and the workspace `pyproject.toml`, then run `uv sync`.

The command refuses shared baseline members. Edit `repos.txt` deliberately to change the team's baseline. It also refuses external or custom checkout locations, which must be managed explicitly.

Removal requires a clean Git checkout with an `origin` remote and no uncommitted changes or unpushed commits. The command always asks for confirmation before deleting anything:

```bash
ws-remove owner/personal-project
ws-remove personal-project  # bare folder name also works if the directory exists
```

### `ws-releases`

Report repos with commits since their newest GitHub release:

```bash
ws-releases                 # at the workspace root: sweep every repo in repos.txt
ws-releases solveit         # only solveit's transitive workspace dependencies
ws-releases --nodeps        # inside a repo: just that repo
ws-releases --skip 'wip'    # extra start-of-message regex for commits that need no release
```

Run from inside a workspace checkout to check that repo and its transitive workspace dependencies. `--nodeps` checks that repo alone and raises an error when no project is selected.

Each pending repo lists its unreleased commit summaries. Repos with no releases yet get a `no releases:` line. Fully released repos appear in `up to date:`.

The newest release is selected by version number, since publish timestamps can be out of order. Each repo's configured default branch is used, whether or not it is `main`.

Commits whose message matches a start-anchored regex from the skip set need no release and aren't reported. `DEFAULT_SKIP` in `fastws.releases` defines the built-in set for version bumps and housekeeping, including `bump`, `nbdev regen`, `.gitignore`, `docs`, and `CI`.

Use `[tool.fastws]` in the workspace root `pyproject.toml` to add skip patterns. Its `release_exclude` list omits entire repos, such as applications that deploy instead of publishing releases:

```toml
[tool.fastws]
release_skip = ["docs only"]
release_exclude = ["solveit", "md_site"]
```

The CLI wraps the Python interface in `fastws.releases`:

```python
from fastws import check_releases, check_release
await check_releases()            # ReleaseReport: the repr is the report
await check_releases('solveit')   # dependency-closure mode
await check_release('mdhtml')     # one repo: list of unreleased commit summaries (None = no releases)
```

## Tests

Run `pytest -q` for the regular suite. `pytest -q tests/test_setup.py -m slow` also builds the current package and bootstraps a fresh environment with real Git and uv; it may fetch package dependencies. Run `chkstyle fastws tests` after edits.
