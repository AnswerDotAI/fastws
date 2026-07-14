"Fast workspace tools for multi-repo management."

__all__ = ["ws_clone", "ws_pull", "ws_status", "ws_branches", "ws_sync", "ws_add", "ws_remove"]

import ast, fnmatch, json, os, re, shutil, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastcore.script import call_parse
from fastgit import Git

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib

def _load_repos(repos_file: str = "repos.txt") -> list[str]:
    p = Path(repos_file)
    if not p.exists(): raise SystemExit(f"File not found: {repos_file}")
    return [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]

def _repo_dir(repo: str) -> str: return repo.split("/")[-1]

def _resolve_path(root: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else root/p

def _repo_key(repo: str) -> str: return repo.strip().rstrip("/").removesuffix(".git").casefold()

def _pkg_key(name: str) -> str: return name.casefold()

def _dep_key(dep: str) -> str:
    dep = dep.split(";", 1)[0].strip()
    dep = re.split(r"[\s<>=!~]", dep, maxsplit=1)[0]
    return dep.split("[", 1)[0].casefold()

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

def _matches_ws(name: str, pattern: str) -> bool:
    pattern = pattern.strip()
    return any(fnmatch.fnmatch(candidate, normalized) for candidate in (name, f"./{name}") for normalized in (pattern, pattern.removeprefix("./")))

def _is_ws_dir(d: Path, members, exclude) -> bool:
    return d.is_dir() and not d.name.startswith(".") and any(_matches_ws(d.name, o) for o in members) and not any(_matches_ws(d.name, o) for o in exclude)

def _ws_dirs(root: Path) -> list[Path]:
    members, exclude = _ws_cfg(root)
    return [d for d in sorted(root.iterdir()) if _is_ws_dir(d, members, exclude)]

def _discover_ws_repos(root: Path) -> list[str]:
    repos = []
    for d in (o for o in _ws_dirs(root) if (o/'.git').exists()):
        try: res = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError: continue
        if repo := _parse_github_repo(res.stdout): repos.append(repo)
    return repos

def _update_repos_file(repos_path: Path, repos: list[str]) -> list[str]:
    existing = _load_repos(repos_path) if repos_path.exists() else []
    seen = {_repo_key(repo) for repo in existing}
    missing = []
    for repo in repos:
        if (key := _repo_key(repo)) in seen: continue
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
    kept = [l for l in lines if l.startswith("#") or not l.strip() or _repo_key(l) != key]
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
    removed = sorted({_pkg_key(s) for s in sources if _pkg_key(s) in targets} | {_dep_key(d) for d in deps if _dep_key(d) in targets})
    if not removed: return []
    sources = {k:v for k,v in sources.items() if _pkg_key(k) not in targets}
    deps = [d for d in deps if _dep_key(d) not in targets]
    content = _replace_table(content, "tool.uv.sources", "\n".join(f"{k} = {{ workspace = true }}" for k in sources))
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

def _sync_ws_pyproject(pyproject_path: Path, template_path: Path, projects: list[str]) -> list[str]:
    if not pyproject_path.exists():
        if not template_path.exists(): raise SystemExit(f"File not found: {template_path}")
        shutil.copyfile(template_path, pyproject_path)
    content = pyproject_path.read_text()
    data = tomllib.loads(content)
    sources = dict(data.get("tool", {}).get("uv", {}).get("sources", {}))
    source_keys = {_pkg_key(proj) for proj in sources}
    missing = [proj for proj in projects if _pkg_key(proj) not in source_keys]
    if not missing: return []
    for proj in missing: sources[proj] = {"workspace": True}
    deps = list(data.get("project", {}).get("dependencies", []))
    dep_keys = {_dep_key(dep) for dep in deps}
    for proj in missing:
        if _pkg_key(proj) in dep_keys: continue
        deps.append(proj)
        dep_keys.add(_pkg_key(proj))
    source_lines = "\n".join(f"{proj} = {{ workspace = true }}" for proj in sources)
    content = _replace_table(content, "tool.uv.sources", source_lines)
    content = _replace_project_dependencies(content, deps)
    pyproject_path.write_text(content)
    return missing

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

def _clone_one(repo: str, root: str = ".") -> str:
    d = Path(root)/_repo_dir(repo)
    if d.exists(): return
    try:
        subprocess.run(["git", "clone", f"git@github.com:{repo}.git", str(d)], check=True, capture_output=True)
        return f"✓ {d.name}: cloned"
    except subprocess.CalledProcessError as e: return f"✗ {d.name}: {e.stderr.decode().strip()}"

def _pull_one(repo: str, root: str = ".") -> str:
    d = Path(root)/_repo_dir(repo)
    if not d.exists(): return f"✗ {d.name}: directory not found"
    try:
        res = subprocess.run(["git", "-C", str(d), "pull", "-q", "--stat"], check=True, capture_output=True, text=True)
        return f"✓ {d.name}" + (f"\n{res.stdout.strip()}" if res.stdout.strip() else "")
    except subprocess.CalledProcessError as e: return f"✗ {d}: {e.stderr.strip()}"

def _pull(repos: list[str], workers: int = 16, root: str = "."):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for result in as_completed([ex.submit(_pull_one, r, root) for r in repos]): print(result.result())

@call_parse
def ws_clone(
    repos_file: str = "repos.txt",  # File containing repo list (one per line: owner/repo)
    workers: int = 16,  # Number of parallel workers
):
    "Clone all repos from a repos file."
    repos = _load_repos(repos_file)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for result in as_completed([ex.submit(_clone_one, r) for r in repos]):
            if result.result(): print(result.result())

@call_parse
def ws_pull(
    repos_file: str = "repos.txt",  # File containing repo list
    workers: int = 16,  # Number of parallel workers
):
    "Pull updates for all repos."
    _pull(_load_repos(repos_file), workers)

@call_parse
def ws_status(
    repos_file: str = "repos.txt",  # File containing repo list
    branches: bool = False,  # Show unpushed commit details
):
    "Show uncommitted changes and optionally unpushed commit details across repos."
    repos = _load_repos(repos_file)
    for repo in repos:
        d = _repo_dir(repo)
        if not Path(d).exists(): continue
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
    repos = _load_repos(repos_file)
    for repo in repos:
        d = _repo_dir(repo)
        if not Path(d).exists():
            print(f"⚠️  {d}: directory not found")
            continue
        g = Git(d)
        if not g.exists:
            print(f"⚠️  {d}: not a git repo")
            continue
        branch = g.branch(show_current=True).strip()
        print(f"✓ {d}: OK (on {expected})" if branch == expected else f"⚠️  {d}: WARNING (on {branch})")

@call_parse
def ws_sync(
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update from local git remotes
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
    workers: int = 16,  # Number of parallel workers
):
    "Sync workspace metadata and run uv sync -U."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    pyproject_path = _resolve_path(root, pyproject_file)
    template_path = _resolve_path(root, template_file)
    repos = _discover_ws_repos(root)

    if missing_repos := _update_repos_file(repos_path, repos): print(f"Added repos: {', '.join(missing_repos)}")
    _pull(repos, workers=workers, root=str(root))

    if missing_projects := _sync_ws_pyproject(pyproject_path, template_path, _ws_projects(root)): print(f"Added workspace projects: {', '.join(missing_projects)}")

    if bad := [d.name for d in _ws_dirs(root) if not (d/"pyproject.toml").exists()]:
        print(f"⚠️  Skipping uv sync, not Python projects yet (scaffold with e.g. nbdev-new or ship-new, or remove): {', '.join(bad)}")
        return
    subprocess.run(["uv", "sync", "-U"], check=True, cwd=root)

@call_parse
def ws_add(
    repo: str,  # Repo to add (owner/repo to clone), or an existing local folder name
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
):
    "Add a repo to repos.txt (a local folder resolves via its origin remote) and then run ws-sync."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    repo, local = _resolve_add_target(root, repo)
    added = _update_repos_file(repos_path, [repo])
    if added: print(f"Added repo: {repo}")
    else: print(f"Repo already present: {repo}")
    if not local: print(_clone_one(repo, str(root)))
    ws_sync(str(root), repos_file, pyproject_file, template_file)

def _is_repo_spec(repo: str) -> bool:
    repo = repo.strip().rstrip("/").removesuffix(".git")
    return bool(_parse_github_repo(repo) or re.fullmatch(r"[^/\s]+/[^/\s]+", repo))

def _resolve_add_target(root: Path, repo: str) -> tuple[str, bool]:
    "Canonical owner/repo for `repo` and whether it named a local folder, which must be a git repo with a GitHub origin and a pyproject.toml"
    if _is_repo_spec(repo): return _normalize_repo(repo), False
    d = root/repo
    if not d.is_dir(): return _normalize_repo(repo), False  # invalid spec and no such folder: raise the standard error
    if not (d/".git").exists(): raise SystemExit(f"{d.name} is not a git repository: `git init` it and create a remote, e.g. `gh repo create <owner>/{d.name} --source={d} --push`")
    res = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True)
    if res.returncode != 0 or not (parsed := _parse_github_repo(res.stdout)):
        raise SystemExit(f"{d.name} has no GitHub 'origin' remote: create one, e.g. `gh repo create <owner>/{d.name} --source={d} --push`")
    if not (d/"pyproject.toml").exists():
        raise SystemExit(f"{d.name} has no pyproject.toml: scaffold it first (e.g. `nbdev-new` or `ship-new`), then re-run ws-add")
    return parsed, True

def _resolve_removal_target(root: Path, repo: str, repos_path: Path) -> str:
    "Canonical owner/repo for `repo`, matching an existing folder name when `repo` isn't a valid spec."
    if _is_repo_spec(repo): return _normalize_repo(repo)
    if not (root/repo).is_dir(): return _normalize_repo(repo)  # invalid spec and no such folder: raise
    for r in (_load_repos(repos_path) if repos_path.exists() else []):
        if _repo_dir(r).casefold() == repo.casefold(): return r
    if (root/repo/".git").exists():
        res = subprocess.run(["git", "-C", str(root/repo), "remote", "get-url", "origin"], capture_output=True, text=True)
        if res.returncode == 0 and (parsed := _parse_github_repo(res.stdout)): return parsed
    return repo

def _resolve_repo_dir(root: Path, repo: str) -> Path:
    root = root.resolve()
    d = root/_repo_dir(repo)
    if d.is_symlink(): raise SystemExit(f"Refusing to remove {d}: it is a symlink")
    if (rd := d.resolve()) == root or rd.parent != root: raise SystemExit(f"Refusing to remove {d}: not directly inside {root}")
    return d

def _git_out(d: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, text=True).stdout

def _repo_safety_issues(d: Path) -> list[str]:
    if not (d/".git").exists(): return [f"{d.name} is not a git repository"]
    issues = []
    if subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True).returncode != 0:
        issues.append(f"{d.name} has no 'origin' remote")
    if _git_out(d, "status", "--porcelain").strip(): issues.append(f"{d.name} has uncommitted changes")
    if _git_out(d, "log", "--branches", "--not", "--remotes", "--format=%h").strip(): issues.append(f"{d.name} has unpushed commits")
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
