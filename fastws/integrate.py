r"""Build several repos' working trees together as one snapshot, apart from the live trees, and group its compiler messages by owner.

Each run snapshots every repo's working tree as a commit, checks the commits out as worktrees under one root, and gives that root the workspace's Cargo config with the root's own crates patched in. Builds there never see a file halfway through an edit, and never touch the files that people and agents are editing."""
import fnmatch, os, posixpath, re, subprocess, tempfile
from pathlib import Path
from ._toml import tomllib
from .core import _local_crates, _patch_text, _strip_patch_tables, _sync_cargo_patches
from .cargo import check

__all__ = ["snapshot", "integrate", "by_owner", "owners", "errors", "code_count", "code_lines", "record"]


def _git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, env=env, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()


def snapshot(repo):
    "A commit of `repo`'s working tree on top of `HEAD`, with untracked files and without ignored ones. `HEAD` and the index don't change."
    repo = Path(repo).expanduser()
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(d)/"index")}
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "add", "-A", env=env)
        tree = _git(repo, "write-tree", env=env)
    return _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "integration snapshot")


def _checkout(repo, dest, commit):
    "Check `commit` out at `dest`, a detached worktree of `repo` that this adds if it's missing, removing the last run's untracked files."
    if not dest.exists(): return _git(repo, "worktree", "add", "--detach", str(dest), commit)
    _git(dest, "checkout", "--detach", "-f", commit)
    _git(dest, "clean", "-fd")


def _config(root, workspace):
    "Write `root`'s Cargo config: `workspace`'s, with the patches for crates under `root` pointing there."
    held = {pkg["name"] for pkg, _ in _local_crates(root)}
    content = (workspace/".cargo"/"config.toml").read_text()
    kept = {url: {n: s for n, s in entries.items() if n not in held} for url, entries in tomllib.loads(content).get("patch", {}).items()}
    config = root/".cargo"/"config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(_strip_patch_tables(content).rstrip() + "\n\n" + _patch_text(kept) + "\n")
    _sync_cargo_patches(root)


def integrate(
    repos:dict, # Each repo's name and its live checkout; the snapshot goes in `root/<name>`
    root, # Directory holding the snapshot worktrees and their Cargo config
    workspace="~/aai-ws", # Workspace whose Cargo config the root copies
)->dict:
    "Snapshot each of `repos`, check the snapshots out under `root`, and write `root`'s Cargo config. Returns each repo's snapshot commit."
    root = Path(root).expanduser().resolve()
    commits = {}
    for name, repo in repos.items():
        repo = Path(repo).expanduser()
        commits[name] = snapshot(repo)
        _checkout(repo, root/name, commits[name])
    _config(root, Path(workspace).expanduser().resolve())
    return commits


def by_owner(
    diagnostics:dict, # Each repo's name and its `fastws.cargo.Diagnostic`s, with paths relative to the repo, such as one check's errors
    owners:dict, # Each owner's path globs, matched against `<repo>/<path>`
)->dict:
    "Group `diagnostics` by owner, then by file, with each path prefixed by its repo's name. A path that no owner's globs match groups under `None`."
    groups = {}
    for repo, ds in diagnostics.items():
        for d in ds:
            d = d._replace(path=posixpath.normpath(f"{repo}/{d.path}"))
            owner = next((o for o, globs in owners.items() if any(fnmatch.fnmatch(d.path, g) for g in globs)), None)
            groups.setdefault(owner, {}).setdefault(d.path, []).append(d)
    return groups


def owners(
    path, # Markdown file with a `Files` heading (expands `~`)
)->dict:
    "Each owner's path globs from the bullets under `path`'s `Files` heading, one `- Owner: `glob` `glob`` line each, in order, as `by_owner` takes them. The first owner whose globs match a path owns it."
    files = re.split(r"^#+ Files\s*$", Path(path).expanduser().read_text(), maxsplit=1, flags=re.M)[1]
    files = re.split(r"^#", files, maxsplit=1, flags=re.M)[0]
    return {m[1]: re.findall(r"`([^`]+)`", m[2]) for m in re.finditer(r"^- ([^:]+):(.*)$", files, re.M)}


def errors(
    root, # Directory holding the snapshot worktrees (expands `~`)
    repos, # Names of the worktrees under `root` to check, in order
)->dict:
    "Each repo's compiler errors in its own files, from checking its worktree under `root`, as `by_owner` takes them. A check repeats a dependency's errors under a path outside the repo; the dependency's own check reports those."
    root = Path(root).expanduser()
    return {r: [d for d in check(root/r).diagnostics if d.level == "error" and not d.path.startswith(("/", ".."))] for r in repos}


def code_count(
    lines, # Rust source lines in order, such as one file's or one side of a diff hunk
)->int:
    "How many of `lines` are code: neither blank nor comments (`//` lines and `/* */` blocks)."
    n, block = 0, False
    for line in lines:
        s = line.strip()
        if block: block = "*/" not in s
        elif s.startswith("/*"): block = "*/" not in s
        elif s and not s.startswith("//"): n += 1
    return n


def code_lines(
    path, # A directory, such as a crate's `src/` (expands `~`)
)->int:
    "The code lines of the Rust files under `path`, as `code_count` counts them."
    return sum(code_count(f.read_text().splitlines()) for f in Path(path).expanduser().rglob("*.rs"))


def record(
    path, # Markdown file to write
    title:str, # The record's heading, such as "Run 8"
    commits:dict, # Each repo's snapshot commit, as `integrate` returns them
    groups:dict, # Messages by owner and file, as `by_owner` returns them
    edits=(), # Edits made in the snapshot only, one line each
    tests:dict=None, # Each repo's name and its test `Results`, as `fastws.cargo.run_tests` returns them
    sizes:dict=None, # Code lines by label, as `code_lines` counts them, such as each repo's `src/` beside its baselines
    sections:dict=None, # Further sections by heading, each text written as a code block, such as each repo's pytest output
)->dict:
    "Write a run's record: the snapshots, each owner's messages by file as `line: message`, any snapshot-only edits, each test binary's outcome with its failures' output, code-line sizes, and any further `sections`. Returns each owner's message count."
    counts = {o: sum(map(len, files.values())) for o, files in groups.items()}
    lines = [f"# {title}", "", "Snapshots: " + ", ".join(f"{name} `{c[:9]}`" for name, c in commits.items()), ""]
    for owner, files in groups.items():
        lines += [f"## {owner or 'no owner'}: {counts[owner]}", ""]
        for file, ds in files.items(): lines += [f"- `{file}`", *[f"  - {d.line}: {d.message}" for d in ds]]
        lines.append("")
    if edits: lines += ["## Snapshot-only edits", "", *[f"- {e}" for e in edits], ""]
    if tests: lines += ["## Tests", "", *[f"### {name}\n\n```\n{r.summary(None)}\n```\n" for name, r in tests.items()]]
    if sizes: lines += ["## Size (code lines, no comments or blanks)", "", *[f"- {k}: {v:,}" for k, v in sizes.items()], ""]
    for heading, text in (sections or {}).items(): lines += [f"## {heading}", "", f"```\n{text}\n```", ""]
    Path(path).expanduser().write_text("\n".join(lines))
    return counts
