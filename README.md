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

Select Python extras by attaching brackets to the repo name, before any checkout location:

```text
AnswerDotAI/fastws[dev]
owner/repo[dev,test] ~/somewhere
```

These select `[project.optional-dependencies]`, not `[dependency-groups]`. Sync uses the package name from `pyproject.toml`, so `AnswerDotAI/fastws[dev]` installs `fastws-cli[dev]` editably. For a checkout containing several discovered Python packages, the selection applies to each. uv handles the extras and their dependencies.

Extras from duplicate entries in `repos.txt` and `repos-local.txt` are combined. For example, a personal `AnswerDotAI/fastws[dev]` entry adds development extras to a shared `AnswerDotAI/fastws` entry. The lists are authoritative for those packages' extras: each sync updates existing workspace dependencies, including removing extras no longer selected. Put personal extra selections in `repos-local.txt`, not the generated `pyproject.toml`. Existing version constraints, markers, sources, and unrelated dependencies are preserved.

### Shared baseline and personal additions

Keep `repos.txt` tracked in the workspace Git repo. Put personal additions in a gitignored `repos-local.txt`, using the same format. Every command reads both lists. `ws-add` and automatic discovery append only to the local list. Edit the shared list deliberately when changing the team's baseline. Duplicate repo names are matched case-insensitively. Conflicting explicit locations, or two different repos targeting the same directory, cause errors.

`--repos-file myrepos.txt` uses `myrepos-local.txt` alongside it. Either list may be absent. Removing a repo from the baseline does not delete its checkout. The next sync discovers an existing root checkout as a personal addition unless `[tool.fastws].exclude` matches its path. External checkouts also remain on disk; add them to the local list to keep managing them.

The workspace `pyproject.toml` is local generated state. `ws-sync` creates it from the tracked `pyproject.tmpl` when available, or generates a minimal one, then maintains it while preserving personal settings except for the managed repo extras, workspace exclusions, build-isolation list, and `metadata-only` backend config settings. The template supplies starting defaults only: later template edits do not overwrite existing configuration. Ignore these files in the workspace repo (not in its member repos):

```gitignore
/repos-local.txt
/pyproject.toml
/package.json
/pnpm-workspace.yaml
/pnpm-lock.yaml
/node_modules/
/pyrightconfig.json
/uv.lock
/.cargo/
/.fastws-upgraded
/.fastws-cargo-upgraded
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

Bootstrap a new workspace, or check an existing workspace and complete any missing toolchain and `sccache` setup.

```bash
ws-setup                                        # check/configure the active workspace
ws-setup --workspace ~/aai-ws                   # check/configure a specific workspace
ws-setup AnswerDotAI/aai-ws ~/aai-ws            # bootstrap a new workspace
ws-setup AnswerDotAI/private-ws ~/aai-ws --workers 8
ws-setup owner/workspace ~/my-workspace --python 3.13
```

With `repo` and `dest`, `ws-setup` clones the workspace repo into a new directory (which must not exist, including as an empty directory or symlink), creates its `.venv`, installs fastws, clones the workspace repos, installs any missing Rust/WebAssembly/JavaScript prerequisites and `sccache` config, and runs that environment's `ws-sync`. It targets the new environment even when another workspace or uv tool environment is already active, and never changes shell startup files or harness settings. A failed step leaves the checkout in place for inspection.

With no `repo`, `ws-setup` inspects the existing workspace and completes any missing steps idempotently: installing `rustup` and `wasm32-unknown-unknown` when Rust/WebAssembly packages are present, installing `sccache`, installing Vite+ (`vp`) and enabling its managed `node`/`pnpm` shims when JavaScript packages are present, and configuring `sccache` when `SCCACHE_R2` is set.

Set `SCCACHE_R2` to a single R2 URL to configure multilevel local-disk + Cloudflare R2 `sccache` caching:

```bash
export SCCACHE_R2="https://<access_key_id>:<secret_access_key>@<account_id>.r2.cloudflarestorage.com/<bucket>"
ws-setup
```

`ws-setup` writes `[cache.multilevel]` (`disk` + `s3`), `[cache.disk]`, `[cache.s3]`, and the workspace root under `basedirs` into the platform `sccache` config (`~/Library/Application Support/Mozilla.sccache/config` on macOS, `~/.config/sccache/config` on Linux, or `SCCACHE_CONF`), preserving existing `basedirs` and unrelated sections. Credentials are written to an isolated `[r2-sccache]` profile in `~/.aws/credentials` (leaving `[default]` untouched), and `.cargo/rustc-lock` selects that profile for `sccache`. Append `?rw_mode=READ_ONLY` for read-only tokens, omit credentials for public read-only buckets, or add `/<prefix>` after `<bucket>` for a `key_prefix`.

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

Build Python sdists and npm tarballs into `.dists` or a specified output directory. JavaScript packages use the same workspace discovery as sync; a non-private package needs a name and version to produce an npm artifact. Private or versionless packages still participate in builds, and mixed Python/JavaScript packages produce both formats when applicable.

For any selected WebAssembly package (`[tool.fastws].wasm-package`), `ws-build` invokes `cargo develop` (for mixed Maturin repos) and `cargo wasm` first so Cargo's own dependency tracking keeps generated outputs fresh across sibling crates, while unrelated WebAssembly crates outside the selected closure are skipped using static metadata. If all selected artifacts are current, skip JavaScript builds and packaging. Otherwise run the selected packages' declared JavaScript builds in topological order via pnpm (`pnpm -r` or `pnpm --filter`), then check freshness again before packaging. This prepares shared assets before either distribution is packaged without rerunning generators on a no-op build. A generated JavaScript consumer also considers its local JS dependencies and the active root `pnpm-lock.yaml`. Changes to an ordinary Python runtime dependency do not invalidate every dependent sdist. Packaging uses `uv build --sdist` and `vp pm pack`.

Each artifact is staged separately and promoted only after a successful package command. Rebuilding removes older versions of that package, leaving unrelated artifacts alone. A packaging failure reports a warning and does not stop independent packaging jobs; a preparatory build failure stops before packaging. The command exits nonzero on failure.

Progress goes to stderr. On success, the output directory path goes to stdout. Scripts can use `$(ws-build)` to build the packages and obtain that path. Use the distributions with a resolver such as `uv pip install --find-links`, or in a Docker build context that needs unreleased workspace packages:

```bash
ws-build
ws-build --force  # rebuild everything
ws-build --out /tmp/dists
ws-build --project solveit  # only this package and its transitive workspace dependencies
```

`--project` accepts a Python or npm package name. Selection follows local Python runtime/build dependencies and JS runtime/development/optional dependencies, joining both sets for co-located packages. External registered projects and declared nested workspaces participate too. Other packages in the output directory are left untouched. Without it, the whole workspace is selected.

### `cargo develop`

Build a mixed Rust/Python project's extension and opted-in binaries, keep them with its Python package as one build, and install that build in the editable source package and the venv:

```bash
cargo develop
cargo develop --path /path/to/project
cargo develop --profile quick
```

Run from the project root, or give its directory with `--path`. The command reads `python-source`, `module-name`, `manifest-path`, `bindings` and features from `[tool.maturin]`. `[tool.fastws] dev-profile = "quick"` selects a project's default for both `cargo develop` and `fastws.cargo.run_tests`. Without that setting they use `test`. An explicit `--profile NAME` overrides the project default. Mixed projects must produce a `cdylib` and set `python-source`; `bindings = "bin"` projects build and install their binary targets directly into the active venv. Define the selected profile in `Cargo.toml` and keep incremental compilation enabled for the local crate.

The workspace's `[tool.fastws] cargo = "/path/to/cargo"` optionally selects one explicit Cargo executable for fastws builds, checks, metadata and tests, including nextest's subordinate Cargo calls. A missing executable fails without falling back. With no selection, subprocess uses its normal PATH and working-directory rules. Upstream Cargo stays installed and usable; manifests, profiles, features and package selection are unchanged.

Optional workspace `cargo-target-dir` passes through Cargo's `CARGO_TARGET_DIR`. The path is absolute or relative to the fastws workspace; explicit caller/environment values win. Cargo's own `[unstable] root-dir` sets the actual common parent of local source repositories. Enable it with `fastws.core.cargo_setup(workspace, root_dir=True)`; later setup calls and integration snapshots keep it at their actual root, including sibling repositories. Cargo command-line/environment overrides keep their normal precedence. Sharing path-crate artifacts needs a Cargo that uses this root for metadata and source paths and tracks manifest-path dependencies: ordinary Cargo's `-Zroot-dir` alone is insufficient. A source root is not a virtual alias for another checkout.

Set `FASTWS_CARGO_LOG` to a file to append the raw Cargo output already captured by builds, checks and test-compilation/listing calls; no log is created by default.

Make the initial editable installation through `ws-sync` or maturin. Repeat that installation when package metadata or entry points change. Running Python processes keep the extension they loaded.

Cargo must already build a loadable Python extension. For PyO3 extension-module builds on macOS, configure the linker in `build.rs` with `pyo3_build_config::add_extension_module_link_args()`. This command does not inject maturin's extra compiler flags.

For reusable Rust libraries, keep the core crate Python-free and put PyO3 in an unpublished `py/` binding crate. Set the workspace's `default-members = [".", "py"]` and maturin's `manifest-path = "py/Cargo.toml"`. The binding crate should produce `["cdylib", "rlib"]` with `test = false`. Both commands then reuse the ordinary core and binding libraries. Rust unit tests still compile a separate `cfg(test)` executable; integration tests reuse the ordinary library. `cargo develop` does not change the project's Cargo configuration or its test layout.

Set `native-binaries = true` under `[tool.fastws]` to build and install the binary targets of the workspace's default members into the active venv alongside the extension. A binary can live in its own member crate, such as a `cli/` crate beside `py/`, listed in `default-members`. maturin's sdist keeps only the members that the extension depends on, so a wheel built from the sdist lacks a binary from such a crate. Cargo reports the executable paths; there is no separate list of binary names.

`cargo develop` also copies the `data/` subdirectory of `[tool.maturin].data` into the active venv, as a wheel install does. For example, `data/share/jupyter/kernels/<name>/kernel.json` is installed as `share/jupyter/kernels/<name>/kernel.json`, where Jupyter finds the kernel. `cargo develop` installs the binaries it builds, not the staged copies in `scripts/`.

Set `post-develop-module = "package.generator"` under `[tool.fastws]` to run a generator after installing the native extension and binaries, before copying wheel data. It runs with the current Python in a fresh process from the project root, including when Cargo reuses its build. A failure stops `cargo develop` or `ws-sync`. Use it for generated files that need the rebuilt extension's metadata.

`cargo develop` keeps each build it installs in `~/.cache/fastws/builds/<project>/<id>/`, read-only. Files are copied first, then hashed, and hard-linked to blobs named by those captured bytes' sha256. The build id hashes these artifacts and the current digests of inputs listed by Cargo's dep-info. Input digests are observations at capture time, not proof of what the compiler read. The record's `started` gives the build's start time; `edited` flags inputs **possibly modified since build started**, by mtime, including inputs of a first build or newly discovered dependencies. Identical inputs and artifacts reuse their first capture's record.

The installed extension and binaries are symbolic links through `<project>/installed`, which `cargo develop` points at the new build in one step. `fastws.store.builds(root)` gives the newest build records, with input changes since the preceding build, and `fastws.store.resolve(root, id)` resolves a build-id prefix. Each develop deletes builds that are neither installed, among the project's last 20, nor from the last 2 days, then old unlinked blobs.

On Unix, cooperating `cargo develop` runs take a per-project lock through compilation, capture, installation, the post-develop generator and wheel data. Store readers and capture/install operations hold shared store access; collection takes it exclusively. Ordinary Cargo commands do not take the project lock: agents must not run competing raw Cargo builds against that project during develop. Windows installs copies and does not take these locks.

On Unix, every rustc call holds a shared `flock` on `/tmp/aai-ws-timing.lock`, through the rustc wrapper that sync writes (see `ws-sync`). A speed test that takes that lock exclusively stays apart from participating builds and test runs. A timing also holds `/tmp/aai-ws-timing.gate` while it waits for the lock and holds it, and a shared hold passes that file first, so new rustc calls and test runs wait behind the timing. Both shared and exclusive acquisition fail after 5 minutes; work never proceeds unlocked. Python code takes the same lock with `fastws.timelock.timing_lock`: `with timing_lock(exclusive=True):` around a timing, and `with timing_lock():` around a test run. Shared-inside-shared in one thread reuses its hold; each thread otherwise takes its own hold. Any nesting involving exclusive raises `RuntimeError` immediately. An exclusive hold that has waited 30 seconds prints the processes that have the lock's file open. Windows builds and tests run without this timing coordination.

Use `fastws.build_backend` as the build backend of a mixed Rust/Python project (with `fastws-cli>=0.0.29` alongside `maturin>=1.13,<2.0` in `[build-system].requires`) or a `bindings = "bin"` Rust CLI project (`fastws-cli>=0.0.39`). Inside a fastws workspace, `ws-sync` writes `[tool.uv.config-settings-package]` with `metadata-only = "1"` into the workspace `pyproject.toml`, so editable builds through `uv sync` install only Python metadata (and the source path when `python-source` is set) and leave Rust compilation to `cargo develop`, which installs the extension, binaries, and wheel data files. Outside the workspace, including `pip install -e .` in CI and standalone installs, editable builds receive no `metadata-only` setting, stage any `native-binaries`, and delegate to maturin so they compile normally.

For distribution wheels containing the binaries, also set `[tool.maturin].data` to a generated wheel-data directory. With `native-binaries = true`, wheel builds through the backend replace its `scripts/` subdirectory with the current Python-free binaries, then delegate packaging to maturin. Explicit profile, target and feature arguments from maturin's build settings also apply to the binaries. Without `native-binaries`, wheel builds go straight to maturin, and `[tool.maturin].data` is optional.

Direct maturin CLI builds bypass the Python build backend. Stage the binaries first with the shared command:

```bash
cargo stage --profile dist
maturin build --profile dist
```

Pass the same target and feature flags to both commands, including `--no-default-features` when needed. Staging builds binaries only. On macOS it matches Maturin's linker flags through `build.rustflags` or existing `RUSTFLAGS`/`CARGO_ENCODED_RUSTFLAGS`. A macOS target with its own `rustflags` must include `-C link-arg=-undefined -C link-arg=dynamic_lookup` there. Shared crates are reused when the two builds resolve the same dependency features. Adding `--lib` to staging can select a different compilation profile under LTO.

### `cargo wait`

Wait until a Cargo workspace compiles:

```bash
cargo wait
cargo wait --path /path/to/workspace
```

Use it when another process's unfinished edits stop the workspace compiling, such as a second agent working in the same tree. Each check runs `cargo check --all-targets`, whose rustc calls hold the timing lock shared, as every build's do. After a failed check, `cargo wait` prints Cargo's first error line. It checks again once a source file of a local package changes and then stays unchanged for 2 seconds. Local packages are the workspace's members and its path dependencies, such as a sibling crate that `ws-sync` patches in. When a check passes, it prints `compiles` and exits. After `--patience` seconds, 1800 by default, it prints the last check's errors and exits with status 1.

### Rust builds and tests from Python

`fastws.cargo.run_tests` builds native tests once with Cargo, without a time limit, then runs the executables that Cargo reports directly from their package directories. Each binary gets its own `timeout`, 30 seconds by default, excluding compilation. Outside `FASTWS_JOB`, a timed-out binary is reported as a failed suite with `timed_out` set. Inside a marked job, its timeout raises so the job owner can clean up descendants. Source edits between building and running do not trigger another build: the results describe the compiled build, not necessarily the latest working tree. It supports directly runnable native tests, not cross-target runners.

Where a root has `.config/nextest.toml`, preparation uses `cargo nextest list` to build the tests and capture Cargo and binary metadata. The metadata graph includes all features at the native platform, independently of the features selected for compilation. Each `cargo nextest run` consumes that metadata without building or waiting for Cargo's target lock. Nextest runs each test in its own process, every binary at once, with the config's slow-test timeouts and groups. `profiles` lists the nextest profiles to run one after another, `('default',)` unless given. `timeout` bounds each profile's execution, and each binary's results come from nextest's JUnit report, which lists no ignored tests. `--profile NAME` passes to the build as `--cargo-profile NAME`, and `test_args` follow `--`.

`root` can also be a list of roots, which build one after another. Roots without nextest then run their binaries in turn. Each profile then runs at the same time in every root whose config defines it: tests in different roots share a phase, and no phase starts before the previous one ends. Suite names start with the root's directory name, and with several profiles also with the profile, as in `app timed: tests/speed.rs`. A failure ends the run after its profile unless `--no-fail-fast` is given.

The default profile is `[tool.fastws].dev-profile`, or `test` when unset. Explicit `--profile NAME`, `--profile=NAME`, `--release` and `-r` override it. Pass Cargo build/selection options positionally, and test filters and harness options in `test_args`. A failed binary stops the run unless `--no-fail-fast` is given. Results contain compiler diagnostics and each binary's outcomes and captured failures.

```python
from fastws.cargo import run_tests

native = run_tests('.', '--no-fail-fast')
focused = run_tests('.', '--test', 'core', test_args=('test_name', '--nocapture'))
phased = run_tests(['app', 'lib'], profiles=('untimed', 'timed'))
```

Doctests are deliberately not run; keep executable examples in ordinary Rust tests. `run_tests` accepts `env` overrides and `timeout=None` to disable its watchdog. It takes no timing lock around the run: rustc's wrapper owns its shared holds, and tests that time own their exclusive holds.

`fastws.cargo.check` runs any Cargo subcommand, `check --all-targets` by default, and returns the same `Results` without test suites. A failing run is a result, not an exception. Each `Diagnostic` in its `diagnostics` holds the message's path, line, column, level and text, and its error code or lint as `code`, such as `E0308` or `clippy::too_many_lines`. The summary lists errors first. `env` sets environment variables for the run, as for `run_tests`. A subcommand that rejects `--message-format`, such as `cargo llvm-lines`, runs with `diagnostics=False`: its result holds the exit status and output, with no diagnostics.

```python
from fastws.cargo import check

lints = check('.', 'clippy', '--all-targets', '--', '-W', 'clippy::too_many_lines', env={'CLIPPY_CONF_DIR': 'review'})
errors = [d for d in lints.diagnostics if d.level == 'error']
sizes = check('.', 'llvm-lines', '--lib', diagnostics=False).text
```

### Integration snapshots

`fastws.integrate` builds several repos together while people or agents keep editing them. `integrate` snapshots each repo's working tree as a commit (untracked files included, ignored files not), checks each commit out as a detached worktree under one root, and writes the root's Cargo config. `HEAD` and the index of each live repo don't change. The config is the workspace's, with the patches for crates under the root pointing at the root's worktrees, so a build there reads the snapshots and every other local crate from the workspace. Keep the root outside the workspace, where `ws-sync` and uv don't treat its worktrees as members.

`errors` checks each snapshot in turn and keeps the errors in that repo's own files. A repo's check also reports its dependencies' errors, under paths outside the repo, and each dependency's own check already covers those. `owners` reads each owner's path globs from the bullets under a Markdown file's `Files` heading, one ``- Owner: `glob` `glob` `` line each, in order. `by_owner` groups each repo's compiler messages by owner, then by file, and the first owner whose globs match a path owns it. It prefixes each path with its repo's name, so a path into a sibling repo, such as `../rustymath/src/x.rs` from basedpl's check, groups as `rustymath/src/x.rs`. `run_tests` with `--no-fail-fast` runs every test binary, each within its own `timeout`, so a slow or crashing binary doesn't stop the others. `code_count` counts the Rust code lines in a sequence of lines, leaving out blanks and comments. `code_lines` applies it to each Rust file under a directory. `exhash`'s `Journal.stat(count=code_count)` counts the code lines that edits add and remove. `record` writes a run's messages, each repo's test `Results` with each failure's output, and any sizes, as a Markdown record, and returns each owner's message count.

```python
from fastws.cargo import run_tests
from fastws.integrate import by_owner, code_lines, errors, integrate, owners, record

root = '~/git/integration'
commits = integrate({'basedpl': '~/aai-ws/basedpl', 'rustymath': '~/aai-ws/rustymath'}, root)
errs = errors(root, ['rustymath', 'basedpl'])
groups = by_owner(errs, owners('~/aai-ws/basedpl/meta/owners.md'))
tests = {r: run_tests(f'{root}/{r}', '--no-fail-fast') for r in errs if not errs[r]}
sizes = {r: code_lines(f'{root}/{r}/src') for r in errs}
counts = record(f'{root}/run8.md', 'Run 8', commits, groups, tests=tests, sizes=sizes)
```
### `ws-sync`

Syncing proceeds in this order:

1. Pull the workspace repo if it has an upstream, using its normal Git pull configuration. A failed pull stops the sync. fastws does not stash or discard local changes.
2. Read the updated shared and local lists and clone missing repos. Clone failures stop the sync before installation.
3. Pull project updates, using the same changed-origin check as `ws-pull`.
4. Update workspace metadata and install the projects.

By default, `ws-sync` uses the active venv's parent as the workspace root. It creates a missing `pyproject.toml` as described above. GitHub checkouts at the root that are absent from both lists are added to `repos-local.txt`, including non-Python repos. Root checkouts matching `[tool.fastws].exclude` are not automatically added. Directories prefixed with `_` are left alone unless explicitly listed.

Project scanning respects `tool.uv.workspace.members` and `exclude`. If a member directory lacks `pyproject.toml`, such as a fresh empty clone, sync warns and skips the `uv sync` step.

Fastws maintains `tool.uv.workspace.exclude`. It excludes unlisted top-level directories without a valid Python project. It also excludes listed Rust or JavaScript projects without `pyproject.toml`. It also excludes listed project templates, whose `[project].name` still holds a `{placeholder}`, because uv cannot parse that name. For a listed checkout with none of `pyproject.toml`, `Cargo.toml`, or `package.json`, sync warns and skips installation.

Existing globs, entries for missing directories, and entries for checkouts that are still not Python projects are retained. Use `exclude = [...]` under `[tool.fastws]` to specify exclusions the scan cannot infer, such as keeping a valid project out of the workspace. Adding members preserves hand-written `[tool.uv.sources]` entries, including path and Git sources.

Each sync regenerates Cargo overrides in the workspace's `.cargo/config.toml`. `[patch.crates-io]` gets an entry for each local crate, including nested Cargo workspace members, that some local manifest requires from crates.io: by version alone, without `path`, `git` or `workspace`, in any dependency table or `[workspace.dependencies]`. Git dependencies that name local crates get entries under the matching URL. Builds under the workspace root use these local checkouts. Entries pointing outside the root and other configuration sections are preserved.

Sync also defaults `[term] quiet = true` in that file to suppress routine Cargo output and warnings. An explicit `quiet = false` is preserved.

Do not commit a `Cargo.lock` generated under these patches. Its source-less local entries cannot be resolved on another machine.

Sync writes `.cargo/rustc-lock` and makes it Cargo's `rustc-wrapper`. The script runs each rustc call holding the timing lock shared (see `cargo develop` above). Inside the lock it runs the wrapper that the config named before sync first wrote the script, where another tool set one, and otherwise `sccache` when it's installed, so unchanged compilation units are reused across the workspace's otherwise-independent Cargo target directories. When `ws-setup` has configured an isolated `r2-sccache` AWS credentials profile for a shared R2 cache, the script passes `AWS_PROFILE=r2-sccache` to `sccache` (clearing ambient `AWS_*` keys in that child process) so it never collides with AWS CLI or `boto3`. Later syncs keep that inner wrapper. The script costs about 9 ms a rustc call, mostly the Python interpreter's start. If `sccache` isn't installed—or `SCCACHE_R2` is set without matching `sccache` config—sync warns and tells you to run `ws-setup`. On Windows, where the lock does nothing, the config keeps another tool's wrapper, or else names `sccache`.

For an isolated build workspace, `fastws.core.cargo_setup(root)` regenerates these Cargo patches and the same compiler wrapper without pulling, syncing Python or building.

Fastws discovers JavaScript packages directly under the workspace root, explicitly registered external repo roots, paths or globs retained in `pnpm-workspace.yaml`, explicit nested paths declared under `[tool.fastws].js-packages` in a repo's `pyproject.toml`, and generated WebAssembly package directories declared under `[tool.fastws].wasm-package = { name = "...", path = "wasm/pkg" }` (when a repo root's own `package.json` already declares that same package name for live source exports, fastws links the repo root in `pnpm-workspace.yaml` and still builds and packs the `wasm-package` directory in `ws-build`). It also reads each package's own `workspaces` list for declared nested packages. The install, build and distribution graph use these same members; fastws does not scan arbitrary nested package files. Lockfiles do not affect discovery. `[tool.fastws].exclude` matches paths relative to the workspace root. Fastws skips `node_modules` and directories whose names start with `.` or `_`.

Each sync updates `packages` in the root `pnpm-workspace.yaml` (creating it with `linkWorkspacePackages: deep`, `preferWorkspacePackages: true`, and `minimumReleaseAge: 1440` by default) and maintains `private: true` in the root `package.json`. It preserves external paths, globs, and unrelated settings, and reports added and removed packages.

When JavaScript workspace members are present, `ws-sync` reads `vp toolchain --json -g` and populates `devDependencies["vite-plus"]` in `package.json` plus matching `vite@*` and `vitest@*` entries under `overrides` in `pnpm-workspace.yaml`. Optional tracked `pnpm-workspace.shared.yaml` and `package.json.shared` files are merged on top on every sync. Object-valued fields merge one level deep, shared values win conflicts, and arrays replace rather than concatenate. Unrelated local settings remain; removing a shared key does not delete its local value. Use these files to declare `packageManager`, `devEngines.runtime`, shared policies such as `allowBuilds`, or explicit Vite+ overrides.

Before `uv sync`, fastws regenerates `tool.uv.no-build-isolation-package` and `[tool.uv.config-settings-package]` (`metadata-only = "1"`) with the sorted package names of projects using `fastws.build_backend`, including external checkouts. These builds use the shared environment's `fastws-cli` and `maturin`, skip Rust compilation during editable installs inside the workspace, and keep metadata-only wheels separate from compiled editable wheels in uv's cache. Leave global build isolation enabled for other projects.

After `uv sync`, fastws writes `_fastws_pytest.pth` into the workspace venv's `site-packages` to default `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, keeping pytest plugins installed for one repo from leaking into others. Repos enable the plugins they need via `-p` in `addopts` (override for a run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=""`).

After `uv sync`, fastws runs `cargo develop` in mixed Rust/Python projects and `bindings = "bin"` Rust CLI projects without a `package.json`, with up to one project per CPU building concurrently by default. Cargo skips unchanged compilation inputs. Projects using `fastws.build_backend` should keep only packaging inputs in `tool.uv.cache-keys`; Rust source changes are handled by Cargo.

Strictly after `cargo develop` finishes, fastws runs `cargo wasm` in parallel for every repo declaring `[tool.fastws].wasm-package`, so any `post-develop-module` generators have already produced their assets and each generated `wasm/pkg/package.json` exists before JavaScript installation.

Fastws then runs `vp install` at the workspace root (driving pnpm underneath). Native pnpm workspaces link local packages whose versions satisfy their consumers' ordinary dependency ranges; consumer manifests need no local paths or special workspace protocol.

After installation, fastws runs `pnpm -r --if-present --workspace-concurrency=<N> run build` (through Vite+'s managed pnpm) so local dependencies build before their consumers and independent builds share the project-concurrency limit. Packages own their build commands: AnswerUI's is `nbdev-export`, for example. Python sync runs first, so a repo-list entry such as `AnswerDotAI/answerui[dev]` supplies its Python build tools through the normal extras mechanism. No additional install lifecycle hook is needed. Cargo and other build tools retain their own incremental behavior.

After the JavaScript builds finish, fastws runs `cargo develop` in `bindings = "bin"` Rust projects that also have a `package.json`, using the same concurrency limit. Binaries that embed built frontend assets then compile against the updated outputs.

`--build-workers` sets the maximum concurrent projects within the Python-native, WebAssembly, and JavaScript build phases, defaulting to the CPU count; `--build-workers 1` runs each phase serially. It is separate from `--workers`, which controls cloning, pulling and dependency updates. Cargo keeps its own within-project parallelism, so increasing project concurrency also increases CPU and memory demand. Cargo may serialize builds that share a target directory.

When JavaScript workspaces are present, fastws requires Vite+ (`vp`) on `PATH` with managed `node` and `pnpm` shims active. When WebAssembly packages (`[tool.fastws].wasm-package`) are present, fastws also requires rustup's `rustc`/`cargo` proxies and the `wasm32-unknown-unknown` target. Run `ws-setup` to install or configure any missing prerequisites.

At most once per day, sync upgrades Python dependencies with `uv sync -U`. At most once every seven days, when Rust crates are present, it updates the stable toolchain with `rustup update stable` (if `rustup` is on `PATH` and no `rust-toolchain.toml` or `rust-toolchain` exists at the workspace root) and runs a parallel `cargo update` in every member with a `Cargo.toml`. It prints the changes and records each run in its own stamp file inside the workspace's `.git` directory (`fastws-upgraded` and `fastws-cargo-upgraded`). Pass `--upgrade` to force both upgrades regardless of the last run time.

```bash
ws-sync
ws-sync --workspace ~/aai-ws
ws-sync --upgrade
ws-sync --build-workers 1
```

### `ws-add`

Add a repo to `repos-local.txt`, then run `ws-sync`. Repos already in either list are not added again.

The argument determines how fastws locates the checkout:

- `owner/repo` clones the repository.
- An existing local folder uses its `origin` remote to resolve `owner/repo`. It needs a `pyproject.toml`, `Cargo.toml` or `package.json`; Rust-only and JavaScript-only projects need no Python scaffold. The command reports missing project metadata, Git repo or GitHub origin.
- A path outside the workspace root leaves the checkout in place and records its location in the local list. The checkout needs no root `pyproject.toml`. Sync discovers its packages.

```bash
ws-add AnswerDotAI/fastws
ws-add answerdotai/fastws
ws-add fastws  # existing local folder, resolved via its origin remote
ws-add ~/private  # stays where it is, recorded in repos-local.txt with its location
```

### `ws-remove`

Delete one or more personal repo clones and remove them from `repos-local.txt` and the workspace `pyproject.toml`. Run `uv sync` once after processing all repos.

The command refuses shared baseline members. Edit `repos.txt` deliberately to change the team's baseline. It also refuses external or custom checkout locations, which must be managed explicitly.

Removal requires a clean Git checkout with an `origin` remote and no uncommitted changes or unpushed commits. The command validates all targets before changing anything. It asks for confirmation before deleting each checkout. Declining keeps that checkout but still removes its workspace metadata. Repeated targets are processed once:

```bash
ws-remove owner/personal-project
ws-remove personal-project  # bare folder name also works if the directory exists
ws-remove owner/project-one project-two project-three
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

### `ws-news`

Summarize user-visible changes across the workspace. The default reports every stable GitHub release published in the last seven days. Each repo has a heading, linked release tags and dates, and one or two paragraphs generated by fastllm. It always checks the whole workspace unless you select a repo, regardless of the current directory.

```bash
ws-news
ws-news --days 14
ws-news --since 2026-10-01 --until 2026-10-15
ws-news fastllm
ws-news --commits
ws-news --since-last
ws-news --commits --since-last
ws-news --model anthropic/claude-sonnet-5-5
```

Configure GitHub authentication with `GITHUB_TOKEN` and the selected model's credentials, such as `ANTHROPIC_API_KEY` for the default Sonnet model. `--workers` limits concurrent repos and model requests to eight by default. Terminal output uses Rich; redirected output is raw Markdown.

Release notes supply the evidence. Releases with empty notes or only a compare link also use the diff from the preceding released tag in their Git history. Publication timestamps determine which releases appear, not version numbers or tag dates. Drafts and prereleases are omitted.

`--commits` compares default-branch snapshots at the date boundaries and supplies their commit messages and net diff. Both modes fetch refs when they need diffs. They never pull, check out branches, or include uncommitted changes. Dates and timestamps without a timezone use UTC. `--until` is an instant, so a date alone means midnight at the start of that date.

`--since-last` stores a checkpoint for each repo and mode in the workspace's `.git/fastws-news.json`. Its first run uses `--days`, seven by default. Only runs using this flag update checkpoints. Successful repos advance to the run's start time after output; failed repos retain their previous checkpoint. Selecting one repo does not advance the others. This flag cannot be combined with `--since` or `--until`.

Diffs honor Git attributes. To suppress textual patches for `cachy.jsonl`, add `cachy.jsonl -diff` to your global Git attributes file, normally `~/.config/git/attributes`, or to a repo's `.git/info/attributes`. There are no built-in path exclusions. Inputs above `--max-chars`, 160,000 by default, fail without a summary or checkpoint advance. Narrow the date window, configure attributes, or raise the limit. A failed repo does not stop other repos; the command exits with status 1 if any failed.

## Tests

Run `pytest -q`. Keep tests concise and focused on substantive logic. Do not test policy choices, settings, constants, or orchestration. Do not add tests merely because code changed.
