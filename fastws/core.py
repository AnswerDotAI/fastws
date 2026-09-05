"Fast workspace tools for multi-repo management."

__all__ = ["ws_clone", "ws_pull", "ws_status", "ws_branches", "ws_build", "ws_sync", "ws_add", "ws_remove"]

import ast, fnmatch, hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastcore.parallel import parallel_async_gen
from fastcore.script import call_parse
from fastgit import Git
from ghapi.core import dep_key

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib

def _repo_lines(repos_file) -> list[str]:
    p = Path(repos_file)
    if not p.exists(): raise SystemExit(f"File not found: {repos_file}")
    return [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]

def _parse_repo_line(line: str) -> tuple[str, str|None]:
    "Split a repos.txt line into its repo spec and optional checkout location"
    parts = line.split(None, 1)
    return parts[0], (parts[1].strip() or None) if len(parts) > 1 else None

def _load_repos(repos_file: str = "repos.txt") -> list[str]:
    return [_parse_repo_line(l)[0] for l in _repo_lines(repos_file)]

def _load_repo_entries(repos_file, root: Path) -> list[tuple[str, Path]]:
    "(repo spec, checkout dir) per repos.txt line; a location expands `~` and resolves relative to `root`, defaulting to `root/<name>`"
    res = []
    for line in _repo_lines(repos_file):
        repo, loc = _parse_repo_line(line)
        res.append((repo, _resolve_path(root, Path(loc).expanduser()) if loc else root/_repo_dir(repo)))
    return res

def _repo_dir(repo: str) -> str: return repo.split("/")[-1]

def _resolve_path(root: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else root/p

def _repo_key(repo: str) -> str: return repo.strip().rstrip("/").removesuffix(".git").casefold()

def _pkg_key(name: str) -> str: return name.casefold()

def _fmt_toml_val(v) -> str:
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, str): return json.dumps(v)
    raise SystemExit(f"Unsupported value in [tool.uv.sources]: {v!r}")

def _fmt_source(src: dict) -> str:
    "A `[tool.uv.sources]` value as a TOML inline table, e.g. `{ path = \"../x\", editable = true }`"
    return "{ " + ", ".join(f"{k} = {_fmt_toml_val(v)}" for k,v in src.items()) + " }"

def _ws_root(workspace: str = "", repos_file: str = "repos.txt", pyproject_file: str = "pyproject.toml",
    template_file: str = "pyproject.tmpl") -> Path:
    if workspace: return Path(workspace).expanduser().resolve()
    for env_name in "UV_PROJECT_ENVIRONMENT","VIRTUAL_ENV":
        if not (env := os.environ.get(env_name)): continue
        root = Path(env).expanduser().resolve().parent
        if any((_resolve_path(root, repos_file).exists(), _resolve_path(root, pyproject_file).exists(), _resolve_path(root, template_file).exists())):
            return root
    return Path.cwd().resolve()

def _parse_github_repo(remote: str) -> str|None:
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", remote.strip())
    return f"{m['owner']}/{m['repo']}" if m else None

def _normalize_repo(repo: str) -> str:
    repo = repo.strip().rstrip("/").removesuffix(".git")
    if parsed := _parse_github_repo(repo): return parsed
    if re.fullmatch(r"[^/\s]+/[^/\s]+", repo): return repo
    raise SystemExit(f"Invalid repo: {repo}. Expected owner/repo or GitHub URL")

def _ws_cfg(root: Path):
    pyproject = root/"pyproject.toml"
    if not pyproject.exists(): return ["./*"], []
    try: data = tomllib.loads(pyproject.read_text())
    except tomllib.TOMLDecodeError: return ["./*"], []
    ws = data.get("tool", {}).get("uv", {}).get("workspace", {})
    members = ws.get("members") or ["./*"]
    exclude = ws.get("exclude") or []
    return members, exclude

def _fastws_cfg(root: Path) -> dict:
    "The `[tool.fastws]` table from the workspace root pyproject.toml (empty when absent)."
    pyproj = root/"pyproject.toml"
    if not pyproj.exists(): return {}
    return tomllib.loads(pyproj.read_text(encoding="utf-8")).get("tool", {}).get("fastws", {})

def _matches_ws(name: str, pattern: str) -> bool:
    pattern = pattern.strip()
    return any(fnmatch.fnmatch(candidate, normalized) for candidate in (name, f"./{name}") for normalized in (pattern, pattern.removeprefix("./")))

def _is_ws_dir(d: Path, members, exclude) -> bool:
    return d.is_dir() and not d.name.startswith(".") and any(_matches_ws(d.name, o) for o in members) and not any(_matches_ws(d.name, o) for o in exclude)

def _ws_dirs(root: Path) -> list[Path]:
    members, exclude = _ws_cfg(root)
    return [d for d in sorted(root.iterdir()) if _is_ws_dir(d, members, exclude)]

def _root_git_dirs(root: Path) -> list[Path]:
    "Every git checkout directly under `root`, whatever the uv workspace config; `_`-prefixed dirs are private"
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_")) and (d/".git").exists()]

async def _discover_ws_repos(root: Path) -> list[str]:
    dirs = _root_git_dirs(root)
    async def origin(d):
        url = await Git(d, sync=False).remote("get-url", "origin", mute_errors=True)
        return _parse_github_repo(url) if url else None
    res = [None]*len(dirs)
    async for i, r in parallel_async_gen(origin, dirs, n_workers=32): res[i] = r
    return [r for r in res if r]

def _update_repos_file(repos_path: Path, repos: list[str]) -> list[str]:
    existing = _load_repos(repos_path) if repos_path.exists() else []
    seen = {_repo_key(repo) for repo in existing}
    missing = []
    for repo in repos:
        if (key := _repo_key(_parse_repo_line(repo)[0])) in seen: continue
        seen.add(key)
        missing.append(repo)
    if not missing: return []
    content = repos_path.read_text() if repos_path.exists() else ""
    if content and not content.endswith("\n"): content += "\n"
    repos_path.write_text(content + "\n".join(missing) + "\n")
    return missing

def _remove_from_repos_file(repos_path: Path, repo: str) -> bool:
    if not repos_path.exists(): return False
    key = _repo_key(repo)
    lines = repos_path.read_text().splitlines()
    kept = [l for l in lines if l.startswith("#") or not l.strip() or _repo_key(_parse_repo_line(l.strip())[0]) != key]
    if len(kept) == len(lines): return False
    repos_path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return True

def _remove_from_pyproject(pyproject_path: Path, names: list[str]) -> list[str]:
    if not pyproject_path.exists(): return []
    content = pyproject_path.read_text()
    data = tomllib.loads(content)
    sources = dict(data.get("tool", {}).get("uv", {}).get("sources", {}))
    deps = list(data.get("project", {}).get("dependencies", []))
    targets = {_pkg_key(n) for n in names}
    removed = sorted({_pkg_key(s) for s in sources if _pkg_key(s) in targets} | {dep_key(d) for d in deps if dep_key(d) in targets})
    if not removed: return []
    sources = {k:v for k,v in sources.items() if _pkg_key(k) not in targets}
    deps = [d for d in deps if dep_key(d) not in targets]
    content = _replace_table(content, "tool.uv.sources", "\n".join(f"{k} = {_fmt_source(v)}" for k,v in sources.items()))
    content = _replace_project_dependencies(content, deps)
    pyproject_path.write_text(content)
    return removed

def _read_pyproject_name(path: Path) -> str|None:
    try: data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        print(f"Skipping invalid TOML: {path}")
        return None
    name = data.get("project", {}).get("name")
    return name if isinstance(name, str) and name and "{" not in name and "}" not in name else None

def _ws_projects(root: Path) -> list[str]:
    return [name for d in (o for o in _ws_dirs(root) if (o/"pyproject.toml").exists())
        if (name := _read_pyproject_name(d/"pyproject.toml"))]

def _external_projects(root: Path, dirs: list[Path]) -> list[tuple[str,str]]:
    "(name, relpath from `root`) for each valid Python project in `dirs`: the dir itself if it is one, else its immediate subdirs"
    res = []
    for d in dirs:
        if not d.is_dir(): continue
        cands = [d] if (d/"pyproject.toml").exists() else sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith((".","_")) and (p/"pyproject.toml").exists())
        for c in cands:
            if name := _read_pyproject_name(c/"pyproject.toml"): res.append((name, os.path.relpath(c, root)))
    return res

def _valid_project_dir(d: Path) -> bool:
    return (d/"pyproject.toml").exists() and bool(_read_pyproject_name(d/"pyproject.toml"))

def _cargo_only(d: Path) -> bool:
    "A Rust crate with no Python layer: not a pending scaffold, just not a uv workspace member"
    return (d/"Cargo.toml").exists() and not (d/"pyproject.toml").exists()

def _npm_only(d: Path) -> bool:
    "A JS package with no Python layer: not a pending scaffold, just not a uv workspace member"
    return (d/"package.json").exists() and not (d/"pyproject.toml").exists()

def _pending_dirs(root: Path) -> list[str]:
    "uv workspace dirs that are not Python projects yet; sync stops rather than let uv fail on them"
    return [d.name for d in _ws_dirs(root) if not (d/"pyproject.toml").exists()]

def _sync_ws_excludes(pyproject_path: Path, root: Path, tracked: set[str]) -> tuple[list[str], list[str]]:
    """Regenerate `tool.uv.workspace.exclude` and return (added, removed).

    Kept as-is: `[tool.fastws].exclude` entries (intent), globs, missing dirs, and tracked dirs
    (repos.txt checkouts) that are still not valid Python projects. Auto-managed: entries for other
    existing dirs are regenerated each sync, excluding dirs without a valid pyproject (tracked dirs
    only when they are Cargo-only crates or npm-only packages, since a tracked dir with none of those files is a pending member
    awaiting scaffolding) and un-excluding dirs that gained one; deliberately excluding a real
    project takes a `[tool.fastws]` entry."""
    if not pyproject_path.exists(): return [], []
    content = pyproject_path.read_text()
    data = tomllib.loads(content)
    ws = data.get("tool", {}).get("uv", {}).get("workspace", {})
    members = ws.get("members") or ["./*"]
    cur = list(ws.get("exclude") or [])
    intent = [e for e in data.get("tool", {}).get("fastws", {}).get("exclude", []) if isinstance(e, str)]
    kept = [e for e in cur if e in intent or any(c in e for c in "*?[") or not (root/e).is_dir() or (e in tracked and not _valid_project_dir(root/e))]
    kept += [e for e in intent if e not in kept]
    auto = [d.name for d in sorted(root.iterdir())
            if d.is_dir() and not d.name.startswith(".") and any(_matches_ws(d.name, m) for m in members)
            and not any(_matches_ws(d.name, e) for e in kept)
            and (d.name not in tracked or _cargo_only(d) or _npm_only(d)) and not _valid_project_dir(d)]
    survivors = set(kept) | set(auto)
    new = [e for e in cur if e in survivors] + [e for e in kept + auto if e not in cur]
    if new == cur: return [], []
    content = _replace_ws_excludes(content, new)
    tomllib.loads(content)  # never write an unparseable pyproject
    pyproject_path.write_text(content)
    return [e for e in new if e not in cur], [e for e in cur if e not in new]

def _replace_ws_excludes(content: str, excludes: list[str]) -> str:
    block = "exclude = [\n" + "".join(f'    "{e}",\n' for e in excludes) + "]"
    if not (span := _table_span(content, "tool.uv.workspace")):
        return content.rstrip() + "\n\n[tool.uv.workspace]\n" + block + "\n"
    start,end = span
    section = content[start:end]
    if m := re.search(r"(?m)^exclude\s*=\s*\[", section):
        arr_start = m.end()-1
        arr_end = _find_array_end(section, arr_start)
        section = section[:m.start()] + block + section[arr_end+1:]
    else: section = section.rstrip() + "\n" + block + "\n"
    return content[:start] + section + content[end:]

def _table_span(content: str, name: str) -> tuple[int,int]|None:
    m = re.search(rf"(?m)^\[{re.escape(name)}\]\s*$", content)
    if not m: return None
    n = re.search(r"(?m)^\[", content[m.end():])
    end = m.end()+n.start() if n else len(content)
    return m.start(), end

def _replace_table(content: str, name: str, body: str) -> str:
    table = f"[{name}]\n{body.rstrip()}\n\n"
    if not (span := _table_span(content, name)): return content.rstrip() + "\n\n" + table
    start,end = span
    return content[:start] + table + content[end:]

def _find_array_end(content: str, start: int) -> int:
    depth = 0
    in_str = escaped = False
    for i,ch in enumerate(content[start:], start):
        if in_str:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == '"': in_str = False
            continue
        if ch == '"': in_str = True
        elif ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: return i
    raise ValueError("Unterminated TOML array")

def _replace_project_dependencies(content: str, deps: list[str]) -> str:
    if not (span := _table_span(content, "project")): raise SystemExit("Missing [project] table in pyproject.toml")
    start,end = span
    section = content[start:end]
    dep_block = "dependencies = [\n" + "".join(f'    "{dep}",\n' for dep in deps) + "]"
    if m := re.search(r"(?m)^dependencies\s*=\s*\[", section):
        arr_start = m.end()-1
        arr_end = _find_array_end(section, arr_start)
        section = section[:m.start()] + dep_block + section[arr_end+1:]
    else: section = section.rstrip() + "\n" + dep_block + "\n"
    return content[:start] + section + content[end:]

def _init_ws_pyproject(path: Path, python: str = f"{sys.version_info.major}.{sys.version_info.minor}") -> bool:
    if path.exists(): return False
    major,minor = map(int, python.split(".")[:2])
    name = re.sub(r"[^a-z0-9]+", "-", path.parent.name.casefold()).strip("-") or "workspace"
    path.write_text(f'''[project]
name = "{name}"
version = "0.0.1"
requires-python = ">={major}.{minor},<{major}.{minor+1}"
dependencies = []

[tool.uv.workspace]
members = ["./*"]
''')
    return True

def _sync_ws_pyproject(pyproject_path: Path, template_path: Path, projects: list[str], externals: list[tuple[str,str]] = None) -> list[str]:
    if not pyproject_path.exists():
        if template_path.exists(): shutil.copyfile(template_path, pyproject_path)
        else: _init_ws_pyproject(pyproject_path)
    content = pyproject_path.read_text()
    data = tomllib.loads(content)
    sources = dict(data.get("tool", {}).get("uv", {}).get("sources", {}))
    source_keys = {_pkg_key(proj) for proj in sources}
    missing = [proj for proj in projects if _pkg_key(proj) not in source_keys]
    ext_missing = [(n,p) for n,p in (externals or []) if _pkg_key(n) not in source_keys]
    if not missing and not ext_missing: return []
    for proj in missing: sources[proj] = {"workspace": True}
    for n,p in ext_missing: sources[n] = {"path": p, "editable": True}
    deps = list(data.get("project", {}).get("dependencies", []))
    dep_keys = {dep_key(dep) for dep in deps}
    for proj in missing + [n for n,_ in ext_missing]:
        if _pkg_key(proj) in dep_keys: continue
        deps.append(proj)
        dep_keys.add(_pkg_key(proj))
    source_lines = "\n".join(f"{proj} = {_fmt_source(src)}" for proj,src in sources.items())
    content = _replace_table(content, "tool.uv.sources", source_lines)
    content = _replace_project_dependencies(content, deps)
    pyproject_path.write_text(content)
    return missing + [n for n,_ in ext_missing]

def _editable_mapping(path: Path) -> dict[str,str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(o, ast.Name) and o.id == "MAPPING" for o in node.targets):
            data = ast.literal_eval(node.value)
            if isinstance(data, dict): return {str(k): str(v) for k,v in data.items()}
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "MAPPING":
            data = ast.literal_eval(node.value)
            if isinstance(data, dict): return {str(k): str(v) for k,v in data.items()}
    return {}

def _site_packages(root: Path) -> Path|None:
    envs = []
    if uv_env := os.environ.get("UV_PROJECT_ENVIRONMENT"): envs.append(Path(uv_env))
    envs.append(root/".venv")
    if virtual_env := os.environ.get("VIRTUAL_ENV"): envs.append(Path(virtual_env))
    for env in envs:
        candidates = sorted(env.glob("lib/python*/site-packages")) + sorted(env.glob("Lib/site-packages"))
        if candidates: return candidates[0]
    return None

def _write_pyright_config(root: Path):
    site = _site_packages(root)
    if not site:
        print("No site-packages directory found for editable Pyright paths")
        return
    paths = sorted({str(Path(p).parent) for f in site.glob("__editable__*_finder.py") for p in _editable_mapping(f).values()})
    cfg_path = root / 'pyrightconfig.json'
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg['extraPaths'] = paths
    cfg_path.write_text(json.dumps(cfg, indent=2) + '\n')
    for pth in site.glob('_pyright_editable_*.pth'): pth.unlink()

async def _clone_one(repo: str, d: Path) -> str|None:
    if d.exists(): return
    try:
        await Git(d.parent, sync=False, raise_exc=True).clone(f"git@github.com:{repo}.git", str(d))
        return f"✓ {d.name}: cloned"
    except subprocess.CalledProcessError as e: return f"✗ {d.name}: {e.stderr.strip()}"

async def _pull_one(d: Path) -> str:
    if not d.exists(): return f"✗ {d.name}: directory not found"
    try:
        out = await Git(d, sync=False, raise_exc=True).pull("-q", "--stat")
        return f"✓ {d.name}" + (f"\n{out}" if out else "")
    except subprocess.CalledProcessError as e: return f"✗ {d}: {e.stderr.strip()}"

async def _pull(dirs: list[Path], workers: int = 64):
    async for _, res in parallel_async_gen(_pull_one, dirs, n_workers=workers): print(res)

async def _remote_heads(refs) -> list[str|None]:
    "Head oid on GitHub for each (owner/name, branch)"
    from ghapi.graphql import GhGql
    gql = GhGql()
    return await gql.batch(gql.repo(s).ref(qualifiedName=f"refs/heads/{b}").target.oid for s, b in refs)

async def _changed_dirs(dirs: list[Path]) -> list[Path]:
    "Dirs whose GitHub origin has moved past the local tracking ref, plus any that can't be checked"
    async def info(d):
        try:
            g = Git(d, sync=False, raise_exc=True)
            spec = _parse_github_repo(await g.remote("get-url", "origin"))
            branch = await g.current_branch
            if not spec or not branch: return None
            local = await g.rev_parse(f"origin/{branch}", mute_errors=True, raise_exc=False)
            return (spec, branch, str(local)) if local else None
        except Exception: return None
    infos = [None]*len(dirs)
    async for i, r in parallel_async_gen(info, dirs, n_workers=32): infos[i] = r
    known = [(d, i) for d, i in zip(dirs, infos) if i]
    unknown = [d for d, i in zip(dirs, infos) if not i]
    if not known: return dirs
    heads = await _remote_heads([(s, b) for _, (s, b, _) in known])
    return [d for ((d, (_, _, local)), remote) in zip(known, heads) if remote != local] + unknown

@call_parse
async def ws_clone(
    repos_file: str = "repos.txt",  # File containing repo list (one per line: owner/repo, plus an optional checkout location)
    workers: int = 16,  # Number of parallel workers
):
    "Clone all repos from a repos file."
    entries = _load_repo_entries(repos_file, Path("."))
    async def clone(e): return await _clone_one(*e)
    async for _, res in parallel_async_gen(clone, entries, n_workers=workers):
        if res: print(res)

@call_parse
async def ws_pull(
    repos_file: str = "repos.txt",  # File containing repo list
    workers: int = 64,  # Number of parallel workers
):
    "Pull updates for repos whose GitHub origin has moved (all repos when that can't be checked)."
    dirs = [d for _,d in _load_repo_entries(repos_file, Path("."))]
    try: dirs = await _changed_dirs(dirs)
    except Exception: pass
    await _pull(dirs, workers)

@call_parse
def ws_status(
    repos_file: str = "repos.txt",  # File containing repo list
    branches: bool = False,  # Show unpushed commit details
):
    "Show uncommitted changes and optionally unpushed commit details across repos."
    for repo,d in _load_repo_entries(repos_file, Path(".")):
        if not d.exists(): continue
        g = Git(d)
        if not g.exists: continue
        changes = g.status('-s') or ""
        unpushed = ""
        try:
            if branches: unpushed = g.log('--branches', '--not', '--remotes', format='%h %s') or ""
            elif (branch := g.branch(show_current=True).strip()) in ('main', 'master'): unpushed = g.log('@{upstream}..HEAD', '-1', format='%h %s') or ""
        except Exception: pass
        if changes or unpushed:
            print(f"\n=== {d} ===")
            if changes: print(changes)
            if unpushed: print(unpushed if branches else "unpushed commits")

@call_parse
def ws_branches(
    repos_file: str = "repos.txt",  # File containing repo list
    expected: str = "main",  # Expected branch name
):
    "Check if all repos are on the expected branch."
    for repo,d in _load_repo_entries(repos_file, Path(".")):
        if not Path(d).exists():
            print(f"⚠️  {d}: directory not found")
            continue
        g = Git(d)
        if not g.exists:
            print(f"⚠️  {d}: not a git repo")
            continue
        branch = g.branch(show_current=True).strip()
        print(f"✓ {d}: OK (on {expected})" if branch == expected else f"⚠️  {d}: WARNING (on {branch})")

_BUILD_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist", "build", "target", ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache"}

def _src_mtime(d: Path, skip: set[str] = _BUILD_SKIP_DIRS) -> float:
    "Newest file mtime under `d`, ignoring VCS internals and build outputs (the dir names in `skip`)"
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [o for o in dirnames if o not in skip and not o.endswith(".egg-info")]
        for f in filenames:
            try: newest = max(newest, os.stat(os.path.join(dirpath, f)).st_mtime)
            except OSError: pass
    return newest

def _dist_name(name: str) -> str: return re.sub(r"[-_.]+", "_", name).casefold()

def _sdists_by_pkg(out: Path) -> dict[str, list[Path]]:
    "Existing sdists in `out`, grouped by normalized package name"
    res = {}
    for p in out.glob("*.tar.gz"): res.setdefault(_dist_name(p.name.removesuffix(".tar.gz").rsplit("-", 1)[0]), []).append(p)
    return res

def _build_projects(root: Path, repos_file: str) -> list[tuple[str, Path]]:
    "(name, dir) for every project ws-sync installs: workspace members plus external checkouts"
    repos_path = _resolve_path(root, repos_file)
    entries = _load_repo_entries(repos_path, root) if repos_path.exists() else []
    ext_dirs = [d for _,d in entries if d.resolve().parent != root.resolve()]
    res = [(name, d) for d in _ws_dirs(root) if (d/"pyproject.toml").exists() and (name := _read_pyproject_name(d/"pyproject.toml"))]
    return res + [(n, _resolve_path(root, p)) for n,p in _external_projects(root, ext_dirs)]

@call_parse
def ws_build(
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    out: str = ".dists",  # Output directory for sdists, relative to the workspace root
    repos_file: str = "repos.txt",  # Repo list, for checkouts outside the workspace root
    force: bool = False,  # Rebuild every project, ignoring existing sdists
    workers: int = 16,  # Number of parallel workers
):
    "Build an sdist of each workspace project into `out`, skipping projects unchanged since their last build; superseded versions are pruned. Progress goes to stderr; on success the dists path prints to stdout; exits 1 if any build fails."
    root = _ws_root(workspace, repos_file)
    out_path = _resolve_path(root, out)
    out_path.mkdir(parents=True, exist_ok=True)
    projs = _build_projects(root, repos_file)
    existing = _sdists_by_pkg(out_path)
    def _stale(name, d):
        cur = existing.get(_dist_name(name))
        return not cur or _src_mtime(d) > max(p.stat().st_mtime for p in cur)
    todo = [(n,d) for n,d in projs if force or _stale(n,d)]

    def bld(nd):
        n,d = nd
        res = subprocess.run(["uv", "build", "--sdist", str(d), "-o", str(out_path)], capture_output=True, text=True)
        return n, res.returncode, (res.stdout or "") + (res.stderr or "")

    built = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, code, out_txt in ex.map(bld, todo):
            if code:
                failed += 1
                print(f"⚠️  {name}: build failed\n{out_txt}", file=sys.stderr)
                continue
            built += 1
            for p in sorted(_sdists_by_pkg(out_path).get(_dist_name(name), []), key=lambda p: p.stat().st_mtime)[:-1]: p.unlink()
            print(f"✓ {name}", file=sys.stderr)
    print(f"{built} built, {len(projs)-len(todo)} up to date" + (f", {failed} failed" if failed else ""), file=sys.stderr)
    if failed: raise SystemExit(1)
    print(out_path)
def _upgrade_stamp(root: Path) -> Path:
    "Stamp whose mtime records the last dependency upgrade; kept inside .git (never tracked) when the root is a git repo."
    gitdir = root/".git"
    return gitdir/"fastws-upgraded" if gitdir.is_dir() else root/".fastws-upgraded"

def _should_upgrade(root: Path, max_age: float = 86400) -> bool:
    "True when this workspace hasn't upgraded dependencies within `max_age` seconds."
    stamp = _upgrade_stamp(root)
    return not stamp.exists() or time.time() - stamp.stat().st_mtime > max_age

_CARGO_KEY = Path(".git/fastws-cargo-key")

def _crate_dirs(root: Path) -> list[Path]:
    "Root dirs containing a Cargo.toml: the crate view of the workspace, independent of uv membership"
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_")) and (d/"Cargo.toml").exists()]

def _cargo_patches(root: Path):
    "Local Cargo patches keyed by normalized Git URL and package name, plus their config file."
    config = root/".cargo"/"config.toml"
    if not config.exists(): return {}, None
    data = tomllib.loads(config.read_text())
    patches = {}
    for url, entries in data.get("patch", {}).items():
        for name, spec in entries.items():
            if not isinstance(spec, dict) or not (path := spec.get("path")): continue
            path = Path(path).expanduser()
            patches[(_repo_key(url), name)] = path if path.is_absolute() else (config.parent/path).resolve()
    return patches, config

def _crate_pkgs(d: Path):
    "(package name, dir) for the crate at `d` and its cargo workspace members"
    try: data = tomllib.loads((d/"Cargo.toml").read_text())
    except tomllib.TOMLDecodeError: return
    if name := data.get("package", {}).get("name"): yield name, d
    for pat in data.get("workspace", {}).get("members", []):
        for m in sorted(d.glob(pat)):
            if not (m/"Cargo.toml").exists(): continue
            try: sub = tomllib.loads((m/"Cargo.toml").read_text())
            except tomllib.TOMLDecodeError: continue
            if name := sub.get("package", {}).get("name"): yield name, m

def _local_crates(root: Path) -> dict[str, Path]:
    "Package name -> dir for every crate under `root`, nested cargo workspace members included"
    return {name: d for crate in _crate_dirs(root) for name, d in _crate_pkgs(crate)}

def _git_dep_tables(root: Path, crates: dict[str, Path]) -> dict[str, dict[str, Path]]:
    "Git URL -> {package: local dir} for members' git deps that name a local crate"
    res = {}
    for d in _crate_dirs(root):
        try: data = tomllib.loads((d/"Cargo.toml").read_text())
        except tomllib.TOMLDecodeError: continue
        for deps in _cargo_dep_tables(data):
            for name, spec in deps.items():
                if not isinstance(spec, dict) or not (url := spec.get("git")): continue
                pkg = spec.get("package", name)
                if path := crates.get(pkg): res.setdefault(url, {})[pkg] = path
    return res

def _strip_patch_tables(content: str) -> str:
    "Remove every top-level `[patch.*]` table from TOML `content`"
    while m := re.search(r"(?m)^\[patch[.\"]", content):
        nl = content.find("\n", m.start())
        if nl == -1: return content[:m.start()]
        nxt = re.search(r"(?m)^\[", content[nl+1:])
        end = nl+1+nxt.start() if nxt else len(content)
        content = content[:m.start()] + content[end:]
    return content

def _sync_cargo_patches(root: Path) -> tuple[list[str], list[str]]:
    """Regenerate `[patch]` entries in the workspace `.cargo/config.toml` and return (added, removed).

    Every local crate gets a `[patch.crates-io]` entry, and a member's git dep on a local crate gets
    an entry under that URL, so builds anywhere under `root` use the checkouts: the cargo analog of
    editable installs. Only entries whose path is inside `root` are managed; entries pointing
    elsewhere (and any other config sections) are kept as-is, and a kept entry wins over a generated
    one of the same name."""
    root = root.resolve()
    crates = _local_crates(root)
    desired = {"crates-io": {name: str(d) for name, d in crates.items()}}
    for url, entries in _git_dep_tables(root, crates).items(): desired[url] = {name: str(d) for name, d in entries.items()}
    config = root/".cargo"/"config.toml"
    content = config.read_text() if config.exists() else ""
    old = tomllib.loads(content).get("patch", {}) if content else {}
    def inside(spec):
        if not isinstance(spec, dict) or not (p := spec.get("path")): return False
        p = Path(p).expanduser()
        return (p if p.is_absolute() else config.parent/p).resolve().is_relative_to(root)
    tables = {}
    for url, entries in old.items():
        if foreign := {n: s for n, s in entries.items() if not inside(s)}: tables[url] = foreign
    for url, entries in desired.items():
        cur = tables.setdefault(url, {})
        for name, path in entries.items(): cur.setdefault(name, {"path": path})
    body = "\n".join(
        (f"[patch.{url}]" if url == "crates-io" else f'[patch."{url}"]') + "\n"
        + "".join(f"{name} = {_fmt_source(spec)}\n" for name, spec in sorted(entries.items()))
        for url, entries in tables.items() if entries)
    new = (_strip_patch_tables(content).rstrip() + "\n\n" + body).lstrip() + "\n" if body else _strip_patch_tables(content)
    if new == content: return [], []
    tomllib.loads(new)  # never write an unparseable config
    config.parent.mkdir(exist_ok=True)
    config.write_text(new)
    before = {n for entries in old.values() for n in entries}
    after = {n for entries in tables.values() for n in entries}
    return sorted(after - before), sorted(before - after)

def _sync_cargo_wrapper(root: Path) -> bool:
    "Add sccache to the generated Cargo config when installed, without overriding another wrapper"
    if not (wrapper := shutil.which("sccache")): return False
    config = root/".cargo"/"config.toml"
    content = config.read_text() if config.exists() else ""
    data = tomllib.loads(content) if content else {}
    if data.get("build", {}).get("rustc-wrapper"): return False
    line = f'rustc-wrapper = {json.dumps(wrapper)}\n'
    if match := re.search(r"(?m)^\[build\][^\n]*\n", content): new = content[:match.end()] + line + content[match.end():]
    else: new = content.rstrip() + ("\n\n" if content.strip() else "") + "[build]\n" + line
    tomllib.loads(new)
    config.parent.mkdir(exist_ok=True)
    config.write_text(new)
    return True

def _cargo_dep_tables(data):
    for name in "dependencies","build-dependencies": yield data.get(name, {})
    for target in data.get("target", {}).values():
        for name in "dependencies","build-dependencies": yield target.get(name, {})

def _patched_cargo_deps(crate: Path, patches):
    data = tomllib.loads((crate/"Cargo.toml").read_text())
    for deps in _cargo_dep_tables(data):
        for name, spec in deps.items():
            if not isinstance(spec, dict) or not (url := spec.get("git")): continue
            if path := patches.get((_repo_key(url), spec.get("package", name))): yield path

def _cargo_input_files(crate: Path):
    for name in "Cargo.toml","build.rs":
        if (path := crate/name).exists(): yield path
    src = crate/"src"
    if src.exists(): yield from sorted(path for path in src.rglob("*") if path.is_file())

def _cargo_lock_content(path: Path) -> bytes:
    "Cargo.lock bytes without Cargo's order-unstable bookkeeping for unused patches"
    if not path.exists(): return b""
    return re.sub(r"(?ms)^\[\[patch\.unused\]\]\n.*?(?=^\[\[|\Z)", "", path.read_text()).encode()

def _cargo_key(crate: Path, patches, config):
    h = hashlib.sha256()
    def add_content(content, name):
        h.update(name.encode())
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    def add(path, name): add_content(path.read_bytes() if path.exists() else b"", name)
    add_content(_cargo_lock_content(crate/"Cargo.lock"), "Cargo.lock")
    if config: add(config, ".cargo/config.toml")
    seen = set()
    def add_dep(dep):
        dep = dep.resolve()
        if dep in seen: return
        seen.add(dep)
        for path in _cargo_input_files(dep): add(path, f"{dep.name}/{path.relative_to(dep)}")
        for child in _patched_cargo_deps(dep, patches): add_dep(child)
    for dep in _patched_cargo_deps(crate, patches): add_dep(dep)
    return h.hexdigest()

def _sync_cargo_keys(root: Path):
    "Update content-derived uv keys for workspace crates, without touching unchanged keys."
    patches, config = _cargo_patches(root)
    for crate in _crate_dirs(root):
        key = crate/_CARGO_KEY
        if not key.parent.is_dir(): continue
        value = _cargo_key(crate, patches, config)
        if key.exists() and key.read_text().strip() == value: continue
        key.write_text(value + "\n")

def _cargo_update(root: Path, workers: int = 16):
    "Float each member crate's Cargo.lock to latest matching versions, in parallel; prints what moved."
    crates = _crate_dirs(root)
    if not crates: return

    def upd(d):
        res = subprocess.run(["cargo", "update"], cwd=d, capture_output=True, text=True)
        return d.name, res.returncode, (res.stdout or "") + (res.stderr or "")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, code, out in ex.map(upd, crates):
            if code: print(f"⚠️  {name}: cargo update failed\n{out}")
            elif lines := [l for l in out.splitlines() if l.lstrip().startswith(("Updating ", "Adding ", "Removing ")) and "crates.io index" not in l]:
                print(f"{name}:\n" + "\n".join(lines))

_JS_SKIP = {"node_modules", "pkg"}

def _npm_dirs(root: Path) -> list[Path]:
    "JS members: a root repo dir with a package.json, else its immediate subdirs that have one; `_`-prefixed names and build outputs are skipped"
    def ok(d): return d.is_dir() and not d.name.startswith((".", "_")) and d.name not in _JS_SKIP
    res = []
    for d in sorted(root.iterdir()):
        if not ok(d): continue
        if (d/"package.json").exists(): res.append(d)
        else: res += [p for p in sorted(d.iterdir()) if ok(p) and (p/"package.json").exists()]
    return res

def _sync_ws_package_json(root: Path, members: list[Path]) -> tuple[list[str], list[str]]:
    """Regenerate `workspaces` in the root package.json (created when first needed) and return (added, removed).

    Entries pointing outside `root` and globs are kept as-is. Entries for dirs inside it are regenerated
    from `members`, so the JS install links every discovered package: the npm analog of editable installs."""
    path = root/"package.json"
    data = json.loads(path.read_text()) if path.exists() else {"private": True}
    cur = list(data.get("workspaces") or [])
    kept = [e for e in cur if any(c in e for c in "*?[") or not (root/e).resolve().is_relative_to(root.resolve())]
    new = kept + [e for e in (os.path.relpath(d, root) for d in members) if e not in kept]
    if new == cur: return [], []
    data["workspaces"] = new
    path.write_text(json.dumps(data, indent=2) + "\n")
    return [e for e in new if e not in cur], [e for e in cur if e not in new]

def _js_tool(root: Path) -> str:
    "The JS package manager: `[tool.fastws].js` in the workspace pyproject, default npm (bun and pnpm read the same `workspaces` list)"
    return _fastws_cfg(root).get("js", "npm")

def _js_stale(root: Path, d: Path) -> bool:
    "Does JS member `d` need its build script? Only members with a Cargo.toml build natively, into `pkg/`, which is stale when missing or older than any source in the member's repo (the parent crate included)"
    if not (d/"Cargo.toml").exists(): return False
    pkg, repo = d/"pkg", d if d.parent.resolve() == root.resolve() else d.parent
    return not pkg.exists() or _src_mtime(pkg) < _src_mtime(repo, _BUILD_SKIP_DIRS | _JS_SKIP)

def _sync_js(root: Path, members: list[Path]) -> list[Path]:
    "Install the JS workspace at `root`, then run the build script of each native member whose output is stale (the JS analog of `maturin develop`); returns the members built"
    tool = _js_tool(root)
    subprocess.run([tool, "install"], check=True, cwd=root)
    built = [d for d in members if _js_stale(root, d)]
    for d in built: subprocess.run([tool, "run", "build"], check=True, cwd=d)
    return built

@call_parse
async def ws_sync(
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update from local git remotes
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
    workers: int = 64,  # Number of parallel workers
    upgrade: bool = False,  # Force the once-daily dependency upgrade pass
):
    "Sync workspace metadata and run uv sync; at most once per day (or with `upgrade`), float dependencies with uv sync -U plus cargo update in member crates."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    pyproject_path = _resolve_path(root, pyproject_file)
    template_path = _resolve_path(root, template_file)
    repos = await _discover_ws_repos(root)

    if missing_repos := _update_repos_file(repos_path, repos): print(f"Added repos: {', '.join(missing_repos)}")
    entries = _load_repo_entries(repos_path, root) if repos_path.exists() else []
    ext_dirs = [d for _,d in entries if d.resolve().parent != root.resolve()]
    dirs = [root/_repo_dir(r) for r in repos] + [d for d in ext_dirs if (d/".git").exists()]
    try: dirs = await _changed_dirs(dirs)
    except Exception: pass
    await _pull(dirs, workers=workers)

    added_ex, removed_ex = _sync_ws_excludes(pyproject_path, root, {d.name for _,d in entries if d.resolve().parent == root.resolve()})
    if added_ex: print(f"Auto-excluded from the workspace: {', '.join(added_ex)}")
    if removed_ex: print(f"No longer excluded from the workspace: {', '.join(removed_ex)}")

    if missing_projects := _sync_ws_pyproject(pyproject_path, template_path, _ws_projects(root), _external_projects(root, ext_dirs)): print(f"Added workspace projects: {', '.join(missing_projects)}")

    wrapper_added = _sync_cargo_wrapper(root)
    added_p, removed_p = _sync_cargo_patches(root)
    if added_p: print(f"Cargo patches added: {', '.join(added_p)}")
    if removed_p: print(f"Cargo patches removed: {', '.join(removed_p)}")
    if wrapper_added: print("Cargo builds now use sccache")

    js_members = _npm_dirs(root)
    added_j, removed_j = _sync_ws_package_json(root, js_members)
    if added_j: print(f"JS workspace packages added: {', '.join(added_j)}")
    if removed_j: print(f"JS workspace packages removed: {', '.join(removed_j)}")

    if bad := _pending_dirs(root):
        print(f"⚠️  Skipping uv sync, not Python projects yet (scaffold with e.g. nbdev-new or ship-new, or remove): {', '.join(bad)}")
        return
    up = upgrade or _should_upgrade(root)
    if up: _cargo_update(root, workers=workers)
    _sync_cargo_keys(root)
    subprocess.run(["uv", "sync", "-U"] if up else ["uv", "sync"], check=True, cwd=root)
    if up: _upgrade_stamp(root).touch()
    if js_members and (built := _sync_js(root, js_members)): print(f"JS packages built: {', '.join(os.path.relpath(d, root) for d in built)}")

@call_parse
async def ws_add(
    repo: str,  # Repo to add (owner/repo to clone), or an existing local folder (a path outside the workspace stays where it is)
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
    workers: int = 64,  # Number of parallel workers
):
    "Add a repo to repos.txt (a local folder resolves via its origin remote) and then run ws-sync."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    repo, local, location = _resolve_add_target(root, repo)
    added = _update_repos_file(repos_path, [f"{repo} {location}" if location else repo])
    if added: print(f"Added repo: {repo}")
    else: print(f"Repo already present: {repo}")
    if not local: print(await _clone_one(repo, root/_repo_dir(repo)))
    await ws_sync(str(root), repos_file, pyproject_file, template_file, workers=workers)

def _is_repo_spec(repo: str) -> bool:
    repo = repo.strip().rstrip("/").removesuffix(".git")
    return bool(_parse_github_repo(repo) or re.fullmatch(r"[^/\s]+/[^/\s]+", repo))

def _origin_repo(d: Path) -> str:
    "Canonical owner/repo from `d`'s GitHub origin remote"
    if not (d/".git").exists(): raise SystemExit(f"{d.name} is not a git repository: `git init` it and create a remote, e.g. `gh repo create <owner>/{d.name} --source={d} --push`")
    url = Git(d).remote("get-url", "origin", mute_errors=True)
    if not url or not (parsed := _parse_github_repo(url)):
        raise SystemExit(f"{d.name} has no GitHub 'origin' remote: create one, e.g. `gh repo create <owner>/{d.name} --source={d} --push`")
    return parsed

def _home_relative(loc: str) -> str:
    "Record locations under the home directory in portable `~/...` form"
    p = Path(loc).expanduser()
    try: return "~/" + str(p.relative_to(Path.home()))
    except ValueError: return str(loc)

def _resolve_add_target(root: Path, repo: str) -> tuple[str, bool, str|None]:
    "Canonical owner/repo for `repo`, whether it's already local, and its checkout location when outside the workspace root"
    if repo.startswith(("~", "/", ".")) and (d := Path(repo).expanduser()).is_dir():
        if d.resolve().parent == root.resolve(): repo = d.name  # a path naming an in-tree folder: fall through to the folder rules
        else: return _origin_repo(d), True, _home_relative(repo)
    if _is_repo_spec(repo): return _normalize_repo(repo), False, None
    d = root/repo
    if not d.is_dir(): return _normalize_repo(repo), False, None  # invalid spec and no such folder: raise the standard error
    parsed = _origin_repo(d)
    if not (d/"pyproject.toml").exists():
        raise SystemExit(f"{d.name} has no pyproject.toml: scaffold it first (e.g. `nbdev-new` or `ship-new`), then re-run ws-add")
    return parsed, True, None

def _resolve_removal_target(root: Path, repo: str, repos_path: Path) -> str:
    "Canonical owner/repo for `repo`, matching an existing folder name when `repo` isn't a valid spec."
    if _is_repo_spec(repo): return _normalize_repo(repo)
    if not (root/repo).is_dir(): return _normalize_repo(repo)  # invalid spec and no such folder: raise
    for r in (_load_repos(repos_path) if repos_path.exists() else []):
        if _repo_dir(r).casefold() == repo.casefold(): return r
    if (root/repo/".git").exists():
        url = Git(root/repo).remote("get-url", "origin", mute_errors=True)
        if url and (parsed := _parse_github_repo(url)): return parsed
    return repo

def _resolve_repo_dir(root: Path, repo: str) -> Path:
    root = root.resolve()
    d = root/_repo_dir(repo)
    if d.is_symlink(): raise SystemExit(f"Refusing to remove {d}: it is a symlink")
    if (rd := d.resolve()) == root or rd.parent != root: raise SystemExit(f"Refusing to remove {d}: not directly inside {root}")
    return d

def _repo_safety_issues(d: Path) -> list[str]:
    if not (d/".git").exists(): return [f"{d.name} is not a git repository"]
    g = Git(d, raise_exc=True)
    issues = []
    if g.remote("get-url", "origin", mute_errors=True, raise_exc=False) is None: issues.append(f"{d.name} has no 'origin' remote")
    if g.status("--porcelain"): issues.append(f"{d.name} has uncommitted changes")
    if g.log("--branches", "--not", "--remotes", format="%h"): issues.append(f"{d.name} has unpushed commits")
    return issues

@call_parse
def ws_remove(
    repo: str,  # Repo to remove, e.g. AnswerDotAI/fastws
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
):
    "Remove a repo: delete its clone and drop it from repos.txt and the workspace pyproject."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    pyproject_path = _resolve_path(root, pyproject_file)
    repo = _resolve_removal_target(root, repo, repos_path)
    d = _resolve_repo_dir(root, repo)
    name = _read_pyproject_name(d/"pyproject.toml") if (d/"pyproject.toml").exists() else None
    names = [name] if name else [d.name]
    in_repos = _repo_key(repo) in {_repo_key(r) for r in (_load_repos(repos_path) if repos_path.exists() else [])}
    if not d.exists() and not in_repos: raise SystemExit(f"Nothing to remove for {repo}")
    if d.exists() and (issues := _repo_safety_issues(d)): raise SystemExit("Refusing to remove:\n" + "\n".join(f"  - {i}" for i in issues))
    _remove_from_repos_file(repos_path, repo)
    _remove_from_pyproject(pyproject_path, names)
    if d.exists():
        try: ans = input(f"Remove directory {d}? [y/N] ")
        except EOFError: ans = ""
        if ans.strip().lower() in ("y", "yes"): shutil.rmtree(d)
    subprocess.run(["uv", "sync"], check=True, cwd=root)
