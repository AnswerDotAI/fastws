"Fast workspace tools for multi-repo management."

__all__ = ["ws_setup", "ws_clone", "ws_pull", "ws_status", "ws_branches", "ws_build", "ws_sync", "ws_add", "ws_remove", "cargo_develop", "cargo_stage", "cargo_wait", "cargo_setup"]

import ast, configparser, fnmatch, json, os, re, shlex, shutil, subprocess, sys, tarfile, time
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, unquote, urlsplit

from fastcore.parallel import parallel_async_gen
from fastcore.script import call_parse
from fastgit import Git
from ghapi.core import dep_key
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import yaml

from ._toml import tomllib

def _repo_lines(repos_file) -> list[str]:
    p = Path(repos_file)
    if not p.exists(): raise SystemExit(f"File not found: {repos_file}")
    return [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]

def _parse_repo_line(line: str) -> tuple[str, str|None, set[str]]:
    "Split owner/repo[extra,...] and an optional checkout location."
    m = re.fullmatch(r'([^\s\[\]]+)(?:\[([^\[\]]+)\])?(?:\s+(.+))?', line.strip())
    if not m: raise SystemExit(f'Invalid repo entry: {line}')
    repo, extras, loc = m.groups()
    return repo, loc, {canonicalize_name(e.strip()) for e in extras.split(',')} if extras else set()

def _load_repos(repos_file: str = "repos.txt") -> list[str]:
    return [_parse_repo_line(l)[0] for l in _repo_lines(repos_file)] if Path(repos_file).exists() else []

def _local_repos_path(repos_file) -> Path:
    p = Path(repos_file)
    return p.with_name(f'{p.stem}-local{p.suffix}')

def _load_repo_entries(repos_file, root: Path) -> list[tuple[str, Path, set[str]]]:
    "Shared and local entries, with extras unioned by repo; conflicting checkout locations are errors."
    res, locations = {}, {}
    for p in (Path(repos_file), _local_repos_path(repos_file)):
        if not p.exists(): continue
        for line in _repo_lines(p):
            repo, loc, extras = _parse_repo_line(line)
            key = _repo_key(repo)
            if key in res:
                res[key][2].update(extras)
                if loc is None: continue
            d = _resolve_path(root, Path(loc).expanduser()) if loc else root/_repo_dir(repo)
            resolved = d.resolve()
            if key in res and res[key][1].resolve() != resolved: raise SystemExit(f'Conflicting locations for {repo} in {p}')
            if resolved in locations and locations[resolved] != key:
                raise SystemExit(f'Checkout location {d} is shared by {repo} and {locations[resolved]}')
            res.setdefault(key, (repo, d, extras))
            locations[resolved] = key
    return list(res.values())

def _repo_dir(repo: str) -> str: return repo.split("/")[-1]

def _resolve_path(root: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else root/p

def _repo_key(repo: str) -> str: return repo.strip().rstrip("/").removesuffix(".git").casefold()

def _pkg_key(name: str) -> str: return canonicalize_name(name)

def _toml_key(k: str) -> str: return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)

def _fmt_toml_val(v) -> str:
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, int): return str(v)
    if isinstance(v, str): return json.dumps(v)
    if isinstance(v, list): return "[" + ", ".join(_fmt_toml_val(x) for x in v) + "]"
    raise SystemExit(f"Unsupported TOML value: {v!r}")

def _fmt_source(src: dict) -> str:
    "A TOML inline table, e.g. `{ path = \"../x\", editable = true }`"
    return "{ " + ", ".join(f"{_toml_key(k)} = {_fmt_toml_val(v)}" for k,v in src.items()) + " }"

def _ws_root(workspace: str = "", repos_file: str = "repos.txt", pyproject_file: str = "pyproject.toml",
    template_file: str = "pyproject.tmpl") -> Path:
    if workspace: return Path(workspace).expanduser().resolve()
    for env_name in "UV_PROJECT_ENVIRONMENT","VIRTUAL_ENV":
        if not (env := os.environ.get(env_name)): continue
        root = Path(env).expanduser().resolve().parent
        paths = (repos_file, _local_repos_path(repos_file), pyproject_file, template_file)
        if any(_resolve_path(root, p).exists() for p in paths): return root
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

def _project_dirs(root: Path, manifest="", members=("*",), exclude=()) -> list[Path]:
    "Expand member patterns into unique directories, optionally requiring a manifest; exclusions are relative to root."
    dirs = dict.fromkeys(d for pat in members for d in sorted(root.glob(pat)))
    return [d for d in dirs if d.is_dir() and (not manifest or (d/manifest).is_file())
        and not any(_matches_ws(os.path.relpath(d, root), e) for e in exclude)]

def _ws_dirs(root: Path) -> list[Path]:
    members, exclude = _ws_cfg(root)
    return [d for d in _project_dirs(root, exclude=exclude) if not d.name.startswith(".") and any(_matches_ws(d.name, o) for o in members)]

def _root_git_dirs(root: Path) -> list[Path]:
    "Every git checkout directly under `root`, whatever the uv workspace config; `_`-prefixed dirs are private"
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_")) and (d/".git").exists()]

async def _discover_ws_repos(root: Path) -> list[str]:
    exclude = _fastws_cfg(root).get("exclude", [])
    dirs = [d for d in _root_git_dirs(root) if not any(_matches_ws(d.name, e) for e in exclude)]
    async def origin(d):
        url = await Git(d, sync=False).remote("get-url", "origin", mute_errors=True)
        return _parse_github_repo(url) if url else None
    res = [None]*len(dirs)
    async for i, r in parallel_async_gen(origin, dirs, n_workers=32): res[i] = r
    return [r for r in res if r]

def _update_repos_file(repos_path: Path, repos: list[str]) -> list[str]:
    seen = {_repo_key(repo) for p in (repos_path, _local_repos_path(repos_path)) for repo in _load_repos(p)}
    missing = []
    for repo in repos:
        if (key := _repo_key(_parse_repo_line(repo)[0])) in seen: continue
        seen.add(key)
        missing.append(repo)
    if not missing: return []
    repos_path = _local_repos_path(repos_path)
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
    removed = sorted({_pkg_key(s) for s in sources if _pkg_key(s) in targets} | {_pkg_key(dep_key(d)) for d in deps if _pkg_key(dep_key(d)) in targets})
    if not removed: return []
    sources = {k:v for k,v in sources.items() if _pkg_key(k) not in targets}
    deps = [d for d in deps if _pkg_key(dep_key(d)) not in targets]
    content = _replace_table(content, "tool.uv.sources", "\n".join(f"{k} = {_fmt_source(v)}" for k,v in sources.items()))
    content = _replace_array(content, "project", "dependencies", deps)
    pyproject_path.write_text(content)
    return removed

def _pyproject_name(path: Path) -> str|None:
    "`[project].name` as written, with any `{placeholder}` slots left in"
    try: data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        print(f"Skipping invalid TOML: {path}")
        return None
    name = data.get("project", {}).get("name")
    return name if isinstance(name, str) and name else None

def _is_placeholder(name: str|None) -> bool: return bool(name) and ("{" in name or "}" in name)

def _read_pyproject_name(path: Path) -> str|None:
    "`[project].name`, or None when it is missing or still a template placeholder"
    name = _pyproject_name(path)
    return None if _is_placeholder(name) else name

def _ws_projects(root: Path) -> list[str]:
    return [name for d in (o for o in _ws_dirs(root) if (o/"pyproject.toml").exists())
        if (name := _read_pyproject_name(d/"pyproject.toml"))]

def _external_projects(root: Path, dirs: list[Path]) -> list[tuple[str,str]]:
    "(name, relpath from `root`) for each valid Python project in `dirs`: the dir itself if it is one, else its immediate subdirs"
    res = []
    for d in dirs:
        if not d.is_dir(): continue
        cands = [d] if (d/"pyproject.toml").exists() else [p for p in _project_dirs(d, "pyproject.toml") if not p.name.startswith((".", "_"))]
        for c in cands:
            if name := _read_pyproject_name(c/"pyproject.toml"): res.append((name, os.path.relpath(c, root)))
    return res

def _repo_extras(root: Path, entries) -> dict[str, set[str]]:
    "Package extras selected by the repo lists, including empty selections for removal on sync."
    return {_pkg_key(name): extras for _,d,extras in entries for name,_ in _external_projects(root, [d])}

def _valid_project_dir(d: Path) -> bool: return (d/"pyproject.toml").exists() and bool(_read_pyproject_name(d/"pyproject.toml"))

def _non_python_project(d: Path) -> bool:
    "A Rust or JS project without a Python layer is not a pending Python scaffold."
    return not (d/"pyproject.toml").exists() and any((d/f).exists() for f in ("Cargo.toml", "package.json"))

def _template_dir(d: Path) -> bool:
    "A project template. Its `[project].name` still holds `{placeholder}` slots, so uv cannot parse it."
    return (d/"pyproject.toml").exists() and _is_placeholder(_pyproject_name(d/"pyproject.toml"))

def _pending_dirs(root: Path) -> list[str]:
    "uv workspace dirs that are not Python projects yet; sync stops rather than let uv fail on them"
    return [d.name for d in _ws_dirs(root) if not (d/"pyproject.toml").exists()]

def _sync_ws_excludes(pyproject_path: Path, root: Path, tracked: set[str]) -> tuple[list[str], list[str]]:
    """Regenerate `tool.uv.workspace.exclude` and return (added, removed).

    Kept as-is: `[tool.fastws].exclude` entries (intent), globs, missing dirs, and tracked dirs
    (repos.txt checkouts) that are still not valid Python projects. Auto-managed: entries for other
    existing dirs are regenerated each sync, excluding dirs without a valid pyproject (tracked dirs
    only when they are Cargo-only crates, npm-only packages, or templates with a `{placeholder}` name, since a tracked dir with none of those files is a pending member
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
    auto = [d.name for d in _project_dirs(root, exclude=kept) if not d.name.startswith(".") and any(_matches_ws(d.name, m) for m in members)
        and (d.name not in tracked or _non_python_project(d) or _template_dir(d)) and not _valid_project_dir(d)]
    survivors = set(kept) | set(auto)
    new = [e for e in cur if e in survivors] + [e for e in kept + auto if e not in cur]
    if new == cur: return [], []
    content = _replace_array(content, "tool.uv.workspace", "exclude", new)
    tomllib.loads(content)  # never write an unparseable pyproject
    pyproject_path.write_text(content)
    return [e for e in new if e not in cur], [e for e in cur if e not in new]

def _replace_array(content: str, table: str, key: str, values: list[str]) -> str:
    block = f"{key} = [\n" + "".join(f'    {json.dumps(v)},\n' for v in values) + "]"
    if not (span := _table_span(content, table)): return _replace_table(content, table, block)
    start,end = span
    section = content[start:end]
    if m := re.search(rf"(?m)^{re.escape(key)}\s*=\s*\[", section):
        arr_start = m.end()-1
        arr_end = _find_array_end(section, arr_start)
        section = section[:m.start()] + block + section[arr_end+1:]
    else: section = section.rstrip() + "\n" + block + "\n"
    return content[:start] + section + content[end:]

def _table_span(content: str, name: str) -> tuple[int,int]|None:
    m = re.search(rf"(?m)^[ \t]*\[{re.escape(name)}\][ \t]*(?:#[^\r\n]*)?\r?$", content)
    if not m: return None
    n = re.search(r"(?m)^[ \t]*\[", content[m.end():])
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

def _sync_ws_pyproject(pyproject_path: Path, template_path: Path, projects: list[str], externals: list[tuple] = None,
    extras: dict[str, set] = None) -> list[str]:
    if not pyproject_path.exists():
        if template_path.exists(): shutil.copyfile(template_path, pyproject_path)
        else: _init_ws_pyproject(pyproject_path)
    original = content = pyproject_path.read_text()
    data = tomllib.loads(content)
    sources = dict(data.get("tool", {}).get("uv", {}).get("sources", {}))
    source_keys = {_pkg_key(proj) for proj in sources}
    missing = [proj for proj in projects if _pkg_key(proj) not in source_keys]
    ext_missing = [(n,p) for n,p in (externals or []) if _pkg_key(n) not in source_keys]
    for proj in missing: sources[proj] = {"workspace": True}
    for n,p in ext_missing: sources[n] = {"path": p, "editable": True}
    deps = list(data.get("project", {}).get("dependencies", []))
    dep_keys = {_pkg_key(dep_key(dep)) for dep in deps}
    for proj in projects + [n for n,_ in (externals or [])]:
        if _pkg_key(proj) in dep_keys: continue
        deps.append(proj)
        dep_keys.add(_pkg_key(proj))
    for i,dep in enumerate(deps):
        if (key := _pkg_key(dep_key(dep))) not in (extras or {}): continue
        req = Requirement(dep)
        if {canonicalize_name(e) for e in req.extras} == extras[key]: continue
        req.extras = extras[key]
        deps[i] = str(req)
    if missing or ext_missing:
        source_lines = "\n".join(f"{proj} = {_fmt_source(src)}" for proj,src in sources.items())
        content = _replace_table(content, "tool.uv.sources", source_lines)
    if deps != data.get("project", {}).get("dependencies", []): content = _replace_array(content, "project", "dependencies", deps)
    if content != original: pyproject_path.write_text(content)
    return missing + [n for n,_ in ext_missing]

def _sync_build_backend(pyproject_path: Path, projects: list[tuple]):
    "Regenerate uv's build-isolation and metadata-only config settings for projects using the fastws backend."
    from .build_backend import METADATA_ONLY
    content = pyproject_path.read_text()
    packages = sorted({name for name,d in projects
        if tomllib.loads((d/'pyproject.toml').read_text()).get('build-system', {}).get('build-backend') == 'fastws.build_backend'})
    uv = tomllib.loads(content).get('tool', {}).get('uv', {})
    cur_iso = uv.get('no-build-isolation-package', [])
    cur_cfg = uv.get('config-settings-package', {})
    pkg_keys = {_pkg_key(p): p for p in packages}
    new_cfg = {}
    for pkg, opts in cur_cfg.items():
        if not isinstance(opts, dict): continue
        merged = {k: v for k, v in opts.items() if k != METADATA_ONLY}
        if (key := _pkg_key(pkg)) in pkg_keys:
            merged[METADATA_ONLY] = '1'
            new_cfg[pkg_keys[key]] = merged
        elif merged: new_cfg[pkg] = merged
    for pkg in packages: new_cfg.setdefault(pkg, {METADATA_ONLY: '1'})
    if packages == cur_iso and new_cfg == cur_cfg: return
    if packages != cur_iso: content = _replace_array(content, 'tool.uv', 'no-build-isolation-package', packages)
    if new_cfg != cur_cfg:
        if new_cfg:
            lines = "\n".join(f"{_toml_key(pkg)} = {_fmt_source(new_cfg[pkg])}" for pkg in sorted(new_cfg))
            content = _replace_table(content, 'tool.uv.config-settings-package', lines)
        elif span := _table_span(content, 'tool.uv.config-settings-package'): content = content[:span[0]] + content[span[1]:]
    tomllib.loads(content)
    pyproject_path.write_text(content)

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

def _write_pytest_pth(root: Path):
    site = _site_packages(root)
    if not site: return
    pth = site / '_fastws_pytest.pth'
    line = 'import os; os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")\n'
    if not pth.exists() or pth.read_text() != line: pth.write_text(line)

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

async def _clone_repos(entries, workers):
    async def clone(e): return await _clone_one(e[0], e[1])
    async for _, res in parallel_async_gen(clone, entries, n_workers=workers):
        if res: print(res)
    if missing := [str(d) for _,d,_ in entries if not (d/'.git').exists()]:
        raise SystemExit(f'Could not clone repos (missing Git checkouts): {", ".join(missing)}')

async def _pull_workspace(root: Path):
    if not (root/'.git').exists(): return
    g = Git(root, sync=False, raise_exc=True)
    if await g.rev_parse('--abbrev-ref', '@{upstream}', mute_errors=True, raise_exc=False): await g.pull()

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

def _prepend_path(env: dict, *dirs: Path):
    cur = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    add = [str(d) for d in dirs if d.is_dir() and str(d) not in cur]
    if add: env["PATH"] = os.pathsep.join(add + cur)

def _setup_tools(root: Path, repos_file: str = "repos.txt", env: dict = None) -> tuple[list[str], dict]:
    "Check workspace toolchain and sccache prerequisites, installing or configuring anything missing."
    env = dict(os.environ if env is None else env)
    which = lambda cmd: shutil.which(cmd, path=env.get("PATH", ""))
    actions = []
    has_rust = bool(_crate_dirs(root))
    wasm_pkgs = _wasm_packages(root, repos_file)
    js_members = _js_dirs(root, repos_file)
    if has_rust or wasm_pkgs:
        if not which("rustup") and (wasm_pkgs or not which("cargo")):
            if os.name == "nt": raise SystemExit("Install rustup from https://rustup.rs, then rerun ws-setup.")
            subprocess.run(["sh", "-c", "curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable"], env=env, check=True)
            _prepend_path(env, Path(env.get("CARGO_HOME", Path.home()/".cargo"))/"bin")
            actions.append("installed rustup (stable)")
        if rustup := which("rustup"):
            if wasm_pkgs:
                for tool in "rustc", "cargo":
                    p = which(tool)
                    if not (p and os.path.samefile(p, rustup)):
                        print(f"⚠️  {tool} resolves to {p or 'nothing'} rather than rustup; put $HOME/.cargo/bin first on PATH.")
                installed = subprocess.run([rustup, "target", "list", "--installed"], capture_output=True, text=True, env=env, check=True).stdout.splitlines()
                if "wasm32-unknown-unknown" not in installed:
                    subprocess.run([rustup, "target", "add", "wasm32-unknown-unknown"], env=env, check=True)
                    actions.append("added wasm32-unknown-unknown target")
        if has_rust:
            if not which("sccache"):
                if cargo := which("cargo"):
                    subprocess.run([cargo, "install", "sccache", "--locked"], env=env, check=True)
                    actions.append("installed sccache")
                else: print("⚠️  cargo not found; install Rust first, then rerun ws-setup to install sccache.")
            if _sync_sccache_config(root):
                actions.append("configured sccache for SCCACHE_R2")
                if sccache := which("sccache"):
                    subprocess.run([sccache, "--stop-server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if (lock := root/".cargo/rustc-lock").exists(): lock.touch()  # invalidate Cargo's cached .rustc_info.json
            _sync_cargo_wrapper(root, env=env)
    if js_members:
        if not which("vp"):
            if os.name == "nt": raise SystemExit("Install Vite+ from https://viteplus.dev, then rerun ws-setup.")
            subprocess.run(["sh", "-c", "curl -fsSL https://vite.plus | bash"], env=env, check=True)
            _prepend_path(env, Path.home()/".vite-plus/bin", Path.home()/".local/bin")
            actions.append("installed Vite+ (vp)")
        if vp := which("vp"):
            if any(not ((p := which(t)) and os.path.samefile(p, vp)) for t in ("node", "pnpm")):
                subprocess.run([vp, "env", "on"], env=env, check=True)
                actions.append("enabled Vite+ managed shims")
                for t in ("node", "pnpm"):
                    p = which(t)
                    if not (p and os.path.samefile(p, vp)):
                        print(f"⚠️  {t} still resolves to {p or 'nothing'} rather than vp; put Vite+'s bin dir first on PATH (e.g. ~/.config/vite-plus/env).")
    return actions, env

@call_parse
def ws_setup(
    repo: str = "",  # Workspace repository (owner/repo or GitHub URL) to clone; omit to check/configure an existing workspace
    dest: str = "",  # New workspace directory (must not exist when `repo` is given)
    workspace: str = "",  # Existing workspace root when `repo` is omitted; defaults to active venv parent
    repos_file: str = "repos.txt",  # Repo list used to detect workspace requirements
    python: str = "3.13",  # Python version for a new workspace environment
    workers: int = None,  # Parallel workers for cloning and initial ws-sync; omit to use defaults
):
    "Bootstrap a new workspace, or check and complete missing toolchain and sccache setup on an existing workspace."
    if repo:
        if not dest: raise SystemExit("Destination directory is required when cloning a workspace repository.")
        root = Path(dest).expanduser().absolute()
        if root.exists() or root.is_symlink(): raise SystemExit(f'Destination exists: {root}; run bare `ws-setup` or `ws-sync` for an existing workspace')
        repo = _normalize_repo(repo)
        root.parent.mkdir(parents=True, exist_ok=True)
        Git(root.parent, raise_exc=True).clone(f'git@github.com:{repo}.git', str(root))
        venv = root/'.venv'
        bindir = venv/'bin'
        env = dict(os.environ, VIRTUAL_ENV=str(venv), UV_PROJECT_ENVIRONMENT=str(venv),
            UV_PROJECT=str(root), UV_WORKING_DIR=str(root), PATH=f'{bindir}{os.pathsep}{os.environ["PATH"]}')
        subprocess.run(['uv', 'venv', '--python', python, str(venv)], cwd=root, env=env, check=True)
        subprocess.run(['uv', 'pip', 'install', '--python', str(bindir/'python'), 'fastws-cli>=0.0.14'], cwd=root, env=env, check=True)
        clone = [str(bindir/'ws-clone'), '--repos-file', repos_file]
        if workers: clone += ['--workers', str(workers)]
        subprocess.run(clone, cwd=root, env=env, check=True)
        _, env = _setup_tools(root, repos_file, env=env)
        sync = [str(bindir/'ws-sync'), '--workspace', str(root), '--repos-file', repos_file]
        if workers: sync += ['--workers', str(workers)]
        subprocess.run(sync, cwd=root, env=env, check=True)
        print(f'Workspace ready: {root}\n\nsource {shlex.quote(str(bindir/"activate"))}\ncd {shlex.quote(str(root))}')
        if (root/'aai-coding/SETUP.md').exists(): print('Then ask your coding agent to follow aai-coding/SETUP.md.')
        return
    if dest: raise SystemExit(f'Unexpected destination without repo: {dest}; use --workspace to target an existing workspace')
    root = _ws_root(workspace, repos_file)
    actions, _ = _setup_tools(root, repos_file)
    if actions: print("Setup completed: " + ", ".join(actions))
    else: print(f"Workspace setup up to date: {root}")

@call_parse
async def ws_clone(
    repos_file: str = "repos.txt",  # File containing repo list (one per line: owner/repo, plus an optional checkout location)
    workers: int = 16,  # Number of parallel workers
):
    "Clone missing repos from the shared and local repo lists."
    entries = _load_repo_entries(repos_file, Path("."))
    await _clone_repos(entries, workers)

@call_parse
async def ws_pull(
    repos_file: str = "repos.txt",  # File containing repo list
    workers: int = 64,  # Number of parallel workers
):
    "Pull updates for repos whose GitHub origin has moved (all repos when that can't be checked)."
    dirs = [d for _,d,_ in _load_repo_entries(repos_file, Path("."))]
    try: dirs = await _changed_dirs(dirs)
    except Exception: pass
    await _pull(dirs, workers)

@call_parse
def ws_status(
    repos_file: str = "repos.txt",  # File containing repo list
    branches: bool = False,  # Show unpushed commit details
):
    "Show uncommitted changes and optionally unpushed commit details across repos."
    for repo,d,_ in _load_repo_entries(repos_file, Path(".")):
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
    for repo,d,_ in _load_repo_entries(repos_file, Path(".")):
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

def _src_mtime(d: Path) -> float:
    "Newest file mtime under `d`, ignoring VCS internals and build outputs"
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [o for o in dirnames if o not in _BUILD_SKIP_DIRS and not o.endswith(".egg-info")]
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

def _npm_dists_by_pkg(out: Path) -> dict[str, list[Path]]:
    "Existing npm tarballs, identified by their package metadata rather than ambiguous filename prefixes."
    res = {}
    for p in out.glob('*.tgz'):
        with tarfile.open(p) as archive: name = json.load(archive.extractfile('package/package.json'))['name']
        res.setdefault(name, []).append(p)
    return res

def _build_projects(root: Path, repos_file: str) -> list[tuple[str, Path]]:
    "(name, dir) for every Python project ws-sync installs: workspace members plus external checkouts."
    repos_path = _resolve_path(root, repos_file)
    entries = _load_repo_entries(repos_path, root)
    ext_dirs = [d for _,d,_ in entries if d.resolve().parent != root.resolve()]
    res = [(name, d) for d in _ws_dirs(root) if (d/"pyproject.toml").exists() and (name := _read_pyproject_name(d/"pyproject.toml"))]
    res += [(n, _resolve_path(root, p)) for n,p in _external_projects(root, ext_dirs)]
    return list(dict.fromkeys((name, d.resolve()) for name,d in res))

def _dependency_dirs(start: Path, graph: dict) -> set[Path]:
    "The starting directory and its transitive local dependencies."
    seen, todo = set(), [start]
    while todo:
        d = todo.pop()
        if d in seen: continue
        seen.add(d)
        todo.extend(graph[d] - seen)
    return seen

def _build_selection(root: Path, repos_file: str, project: str = None):
    "Python projects, JS metadata and JS dependency graph for the selected mixed-language closure."
    py = _build_projects(root, repos_file)
    wasm_pkgs = _wasm_packages(root, repos_file)
    packages, js_graph = _js_graph(_js_dirs(root, repos_file), wasm_pkgs)
    graph = {d: set(deps) for d,deps in js_graph.items()}
    names = {_dist_name(n): d for n,d in py}
    for name, d in py:
        data = tomllib.loads((d/'pyproject.toml').read_text())
        deps = data.get('project', {}).get('dependencies', []) + data.get('build-system', {}).get('requires', [])
        graph.setdefault(d, set()).update(names[key] for dep in deps if (key := _dist_name(dep_key(dep))) in names)
        graph[d].update(j for j in _js_package_specs(d) if j in packages)
    if project is None: return py, packages, js_graph
    matches = {d for n,d in py if _dist_name(n) == _dist_name(project)} | {d for d,p in packages.items() if p.get('name') == project}
    if not matches: raise SystemExit(f'No workspace project named {project}')
    if len(matches) > 1: raise SystemExit(f'Ambiguous workspace project {project}: {", ".join(map(str, sorted(matches)))}')
    dirs = _dependency_dirs(matches.pop(), graph)
    return [(n,d) for n,d in py if d in dirs], {d: p for d,p in packages.items() if d in dirs}, {d: deps for d,deps in js_graph.items() if d in dirs}

@contextmanager
def _cli_errors():
    "Exit without a traceback on command failures or missing settings, printing captured stderr and preserving command exit status."
    from .cargo import ConfigError
    try: yield
    except subprocess.CalledProcessError as e:
        if e.stderr: print(e.stderr, file=sys.stderr, end='')
        raise SystemExit(e.returncode) from None
    except ConfigError as e: raise SystemExit(str(e)) from None

@call_parse(pos=['command'])
def cargo_develop(
    command: str = 'develop', # Subcommand name supplied by Cargo
    path: str = '.', # Rust/Python project directory
    profile: str = None, # Cargo profile; default: tool.fastws.dev-profile, or test
):
    r"Build and install a mixed Rust/Python project's extension and opted-in binaries."
    from .cargo import develop
    with _cli_errors(): develop(Path(path).resolve(), profile=profile)

@call_parse(pos=['command'])
def cargo_stage(
    command: str = 'stage', # Subcommand name supplied by Cargo
    path: str = '.', # Rust/Python project directory
    profile: str = '', # Cargo profile; defaults to the maturin profile or dev
    target: str = '', # Cargo target triple
    features: str = '', # Space or comma separated Cargo features
    all_features: bool = False, # Enable all Cargo features
    no_default_features: bool = False, # Disable default Cargo features
):
    r"Stage Python-free native binaries for maturin wheel builds."
    from .cargo import stage_binaries
    with _cli_errors():
        for dest in stage_binaries(Path(path).resolve(), profile or None, target or None, features,
            all_features, no_default_features): print(f'Staged {dest}')

def _newest(paths):
    "Newest file mtime among `paths`, walking each directory as `_src_mtime` does"
    return max((_src_mtime(p) if p.is_dir() else p.stat().st_mtime) for p in paths if p.exists())

@call_parse(pos=['command'])
def cargo_wait(
    command: str = 'wait', # Subcommand name supplied by Cargo
    path: str = '.', # Cargo workspace directory
    patience: int = 1800, # Seconds to wait before giving up
):
    r"Wait until a Cargo workspace compiles, checking again after each edit to its local packages' sources."
    from .cargo import check, sources
    root, start = Path(path).resolve(), time.monotonic()
    with _cli_errors(): srcs = sources(root)
    while (res := check(root)).returncode:
        if time.monotonic() - start > patience: raise SystemExit(f'{res.text}gave up after {patience} s')
        print(next((str(d) for d in res.diagnostics if d.level == 'error'), res.text.strip() or 'cargo check failed'), file=sys.stderr)
        before = _newest(srcs)
        while (now := _newest(srcs)) == before and time.monotonic() - start < patience: time.sleep(1)
        while True:
            time.sleep(2)
            if (later := _newest(srcs)) == now: break
            now = later
    print('compiles')

@call_parse
def ws_build(
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    out: str = ".dists",  # Output directory for Python sdists and npm tarballs, relative to the workspace root
    repos_file: str = "repos.txt",  # Repo list, for checkouts outside the workspace root
    force: bool = False,  # Rebuild every selected distribution, ignoring existing artifacts
    workers: int = 16,  # Number of parallel workers
    project: str = None,  # Build only this package and its transitive workspace dependencies
):
    "Build Python sdists and npm tarballs, preparing generated JS assets first; unchanged artifacts are skipped and superseded versions pruned."
    root = _ws_root(workspace, repos_file)
    out_path = _resolve_path(root, out)
    out_path.mkdir(parents=True, exist_ok=True)
    wasm_pkgs = _wasm_packages(root, repos_file)
    py, packages, js_graph = _build_selection(root, repos_file, project)
    selected_wasm = {repo: (name, pkg_dir) for repo, (name, pkg_dir) in wasm_pkgs.items()
        if pkg_dir in packages or (repo in packages and packages[repo].get('name') == name)}
    wasm_owner = {pkg_dir: repo for repo, (_, pkg_dir) in selected_wasm.items()}
    existing = {('sdist', n): paths for n,paths in _sdists_by_pkg(out_path).items()}
    existing.update({('npm', n): paths for n,paths in _npm_dists_by_pkg(out_path).items()})
    if selected_wasm:
        _check_rustup(wasm=True)
        from .cargo import develop
        try:
            native_wasm = [repo for repo in selected_wasm if _is_maturin_native(repo)]
            if native_wasm:
                with ThreadPoolExecutor(max_workers=workers) as ex: list(ex.map(develop, native_wasm))
            with ThreadPoolExecutor(max_workers=workers) as ex: list(ex.map(_build_wasm, selected_wasm))
        except subprocess.CalledProcessError as e: raise SystemExit(e.returncode) from None
    wasm_dists = {}
    for repo, (_, pkg_dir) in selected_wasm.items():
        meta = json.loads((pkg_dir/'package.json').read_text())
        if pkg_dir in packages: packages[pkg_dir] = meta
        else: wasm_dists[pkg_dir] = meta
    artifacts = [('sdist', n, d) for n,d in py]
    artifacts += [('npm', p['name'], d) for d,p in packages.items() if p.get('name') and p.get('version') and not p.get('private')]
    artifacts += [('npm', p['name'], d) for d,p in wasm_dists.items() if p.get('name') and p.get('version') and not p.get('private')]
    js_closure = {d: _dependency_dirs(d, js_graph) for d, p in packages.items() if p.get('scripts', {}).get('build')}
    for _, d in py:
        if nested := [j for j in _js_package_specs(d) if j in js_closure]: js_closure[d] = {d}.union(*(js_closure[j] for j in nested))
    lock = root/'pnpm-lock.yaml'
    def _dir_mtime(d):
        t = _src_mtime(d) if d.exists() else 0
        if d in wasm_owner: t = max(t, _src_mtime(wasm_owner[d]))
        if d in selected_wasm and selected_wasm[d][1].exists(): t = max(t, _src_mtime(selected_wasm[d][1]))
        return t
    def stale():
        mtimes = {d: _dir_mtime(d) for d in set(packages) | set(wasm_dists) | {d for _,d in py}}
        lock_mtime = lock.stat().st_mtime if lock.exists() else 0
        for d in js_closure: mtimes[d] = max(mtimes[d], lock_mtime)
        return [(kind, n, d) for kind,n,d in artifacts if force or not (cur := existing.get((kind, _dist_name(n) if kind == 'sdist' else n)))
            or max(mtimes[p] for p in js_closure.get(d, {d})) > max(p.stat().st_mtime for p in cur)]
    todo = stale()
    if todo:
        build_dirs = [d for d,p in packages.items() if p.get('scripts', {}).get('build')]
        if build_dirs:
            _check_vp()
            try: _build_js(root, workers, dirs=build_dirs if project else None)
            except subprocess.CalledProcessError as e: raise SystemExit(e.returncode) from None
            todo = stale()
    if any(kind == 'npm' for kind, _, _ in todo): _check_vp()

    def bld(item):
        kind,n,d = item
        with TemporaryDirectory(prefix='.fastws-', dir=out_path) as tmp:
            if kind == 'sdist': cmd, cwd = ['uv', 'build', '--sdist', str(d), '-o', tmp], d
            else: cmd, cwd = ['vp', 'pm', 'pack', '--pack-destination', tmp, '--', '--dir', str(d)], root
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            dest = None
            if not res.returncode:
                artifact, = Path(tmp).glob('*.tar.gz' if kind == 'sdist' else '*.tgz')
                dest = out_path/artifact.name
                artifact.replace(dest)
            return kind, n, dest, res.returncode, (res.stdout or '') + (res.stderr or '')

    built = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for kind, name, dest, code, out_txt in ex.map(bld, todo):
            if code:
                failed += 1
                print(f"⚠️  {name}: build failed\n{out_txt}", file=sys.stderr)
                continue
            built += 1
            for p in existing.get((kind, _dist_name(name) if kind == 'sdist' else name), []):
                if p != dest: p.unlink()
            print(f"✓ {name}", file=sys.stderr)
    print(f"{built} built, {len(artifacts)-len(todo)} up to date" + (f", {failed} failed" if failed else ""), file=sys.stderr)
    if failed: raise SystemExit(1)
    print(out_path)
def _upgrade_stamp(root: Path, name: str = "fastws-upgraded") -> Path:
    "Stamp whose mtime records the last dependency upgrade; kept inside .git (never tracked) when the root is a git repo."
    gitdir = root/".git"
    return gitdir/name if gitdir.is_dir() else root/f".{name}"

def _should_upgrade(root: Path, max_age: float = 86400, name: str = "fastws-upgraded") -> bool:
    "True when this workspace hasn't upgraded dependencies within `max_age` seconds."
    stamp = _upgrade_stamp(root, name)
    return not stamp.exists() or time.time() - stamp.stat().st_mtime > max_age

def _crate_dirs(root: Path) -> list[Path]:
    "Root dirs containing a Cargo.toml: the crate view of the workspace, independent of uv membership"
    return [d for d in _project_dirs(root, "Cargo.toml") if not d.name.startswith((".", "_"))]

def _crate_manifests(d: Path):
    "(parsed `Cargo.toml`, dir) for the crate or workspace at `d` and each of its cargo workspace members"
    try: data = tomllib.loads((d/"Cargo.toml").read_text())
    except tomllib.TOMLDecodeError: return
    yield data, d
    for m in _project_dirs(d, "Cargo.toml", data.get("workspace", {}).get("members", [])):
        try: sub = tomllib.loads((m/"Cargo.toml").read_text())
        except tomllib.TOMLDecodeError: continue
        yield sub, m

def _crate_pkgs(d: Path):
    "(`[package]` table, dir) for the crate at `d` and its cargo workspace members"
    for data, m in _crate_manifests(d):
        if "name" in (pkg := data.get("package", {})): yield pkg, m

def _local_crates(root: Path) -> list[tuple[dict, Path]]:
    "(`[package]` table, dir) for every crate under `root`, nested cargo workspace members included"
    return [p for crate in _crate_dirs(root) for p in _crate_pkgs(crate)]

def _registry_requirements(root: Path) -> set[str]:
    "Packages that some manifest under `root` requires from crates.io: a version with no `path`, `git`, `registry` or `workspace` key"
    return {spec.get("package", name) if isinstance(spec, dict) else name
        for crate in _crate_dirs(root) for data, _ in _crate_manifests(crate) for deps in _cargo_dep_tables(data, every=True)
        for name, spec in deps.items() if isinstance(spec, str) or isinstance(spec, dict) and not {"path", "git", "registry", "workspace"} & spec.keys()}

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

def _patch_text(tables: dict) -> str:
    "TOML `[patch.*]` tables for `tables`, which maps each source URL to its crates' source specs"
    return "\n".join(
        (f"[patch.{url}]" if url == "crates-io" else f'[patch."{url}"]') + "\n"
        + "".join(f"{name} = {_fmt_source(spec)}\n" for name, spec in sorted(entries.items()))
        for url, entries in tables.items() if entries)

def _entry_changes(current, new):
    "Added and removed keys, in the order supplied."
    return [k for k in new if k not in current], [k for k in current if k not in new]

def cargo_setup(root, *, root_dir=False):
    "Regenerate local Cargo patches and the compile-lock wrapper without pulling or building; root_dir enables Cargo's common source root."
    root = Path(root).expanduser().resolve()
    _sync_cargo_wrapper(root)
    return _sync_cargo_patches(root, root_dir=root_dir)

def _sync_cargo_patches(root: Path, root_dir=False) -> tuple[list[str], list[str]]:
    """Regenerate workspace Cargo patches, default to quiet output, and return (added, removed).

    A local crate gets a `[patch.crates-io]` entry only where some local manifest requires it from crates.io
    (`_registry_requirements`); Cargo uses no other patch. A member's git dep on a local crate gets an entry under
    that URL. Builds anywhere under `root` then use the checkouts: the cargo analog of editable installs. Only
    entries whose path is inside `root` are managed; entries pointing elsewhere (and any other config sections) are
    kept as-is, and a kept entry wins over a generated one of the same name. An enabled `[unstable] root-dir`
    follows the actual root; `root_dir` enables it."""
    root = root.resolve()
    pkgs = _local_crates(root)
    crates = {pkg["name"]: d for pkg, d in pkgs}
    required = _registry_requirements(root)
    desired = {"crates-io": {name: str(d) for name, d in crates.items() if name in required}}
    for url, entries in _git_dep_tables(root, crates).items(): desired[url] = {name: str(d) for name, d in entries.items()}
    config = root/".cargo"/"config.toml"
    original = content = config.read_text() if config.exists() else ""
    data = tomllib.loads(content) if content else {}
    old = data.get("patch", {})
    if root_dir or 'root-dir' in data.get('unstable', {}):
        span = _table_span(content, 'unstable')
        body = content[span[0]:span[1]].partition('\n')[2] if span else ''
        body = re.sub(r'(?m)^[ \t]*root-dir[ \t]*=.*(?:\n|$)', '', body)
        content = _replace_table(content, 'unstable', f'root-dir = {json.dumps(str(root))}\n{body}')
    if "quiet" not in data.get("term", {}):
        if match := re.search(r"(?m)^\[term\][^\n]*(?:\n|$)", content):
            content = content[:match.end()].rstrip("\n") + "\nquiet = true\n" + content[match.end():]
        else: content = _replace_table(content, "term", "quiet = true").lstrip()
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
    body = _patch_text(tables)
    new = (_strip_patch_tables(content).rstrip() + "\n\n" + body).lstrip() + "\n" if body else _strip_patch_tables(content)
    if new == original: return [], []
    tomllib.loads(new)  # never write an unparseable config
    config.parent.mkdir(exist_ok=True)
    config.write_text(new)
    before = {n for entries in old.values() for n in entries}
    after = {n for entries in tables.values() for n in entries}
    added, removed = _entry_changes(before, after)
    return sorted(added), sorted(removed)

_SCCACHE_AWS_PROFILE = "r2-sccache"

def _sccache_config_path() -> Path:
    if conf := os.environ.get("SCCACHE_CONF"): return Path(conf).expanduser()
    if sys.platform == "darwin": return Path.home()/"Library/Application Support/Mozilla.sccache/config"
    if os.name == "nt": return Path(os.environ.get("APPDATA", Path.home()/"AppData/Roaming"))/"Mozilla/sccache/config/config"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home()/".config"))/"sccache/config"

def _sccache_disk_dir() -> Path:
    if d := os.environ.get("SCCACHE_DIR"): return Path(d).expanduser()
    if sys.platform == "darwin": return Path.home()/"Library/Caches/Mozilla.sccache"
    if os.name == "nt": return Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local"))/"Mozilla/sccache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home()/".cache"))/"sccache"

def _aws_creds_path() -> Path: return Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", Path.home()/".aws/credentials")).expanduser()

def _parse_sccache_r2(url: str) -> dict:
    "Parse `SCCACHE_R2` (`https://[key:secret@]<acct>.r2.cloudflarestorage.com/<bucket>[/prefix][?rw_mode=READ_ONLY]`)."
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SystemExit("Invalid SCCACHE_R2 URL: expected https://[key:secret@]<endpoint>/<bucket>")
    parts = [p for p in u.path.strip("/").split("/") if p]
    if not parts: raise SystemExit("Invalid SCCACHE_R2 URL: missing bucket name in path")
    if (u.username is None) != (u.password is None):
        raise SystemExit("Invalid SCCACHE_R2 URL: specify both access key and secret key, or neither")
    endpoint = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    rw_mode = parse_qs(u.query).get("rw_mode", [None])[0]
    access_key = unquote(u.username) if u.username is not None else None
    secret_key = unquote(u.password) if u.password is not None else None
    return dict(endpoint=endpoint, bucket=parts[0], key_prefix="/".join(parts[1:]),
        use_ssl=(u.scheme == "https"), access_key=access_key, secret_key=secret_key, rw_mode=rw_mode)

def _has_aws_profile(profile: str = _SCCACHE_AWS_PROFILE) -> bool:
    path = _aws_creds_path()
    if not path.exists(): return False
    cp = configparser.RawConfigParser()
    cp.read(path)
    return cp.has_section(profile)

def _sync_aws_profile(access_key: str, secret_key: str, profile: str = _SCCACHE_AWS_PROFILE) -> bool:
    "Write or update `profile` in AWS credentials without disturbing other profiles; return True if changed."
    path = _aws_creds_path()
    cp = configparser.RawConfigParser()
    if path.exists(): cp.read(path)
    if cp.has_section(profile) and cp.get(profile, "aws_access_key_id", fallback=None) == access_key \
            and cp.get(profile, "aws_secret_access_key", fallback=None) == secret_key: return False
    if not cp.has_section(profile): cp.add_section(profile)
    cp.set(profile, "aws_access_key_id", access_key)
    cp.set(profile, "aws_secret_access_key", secret_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f: cp.write(f)
    if os.name != "nt": path.chmod(0o600)
    return True

def _set_top_array(content: str, key: str, values: list[str]) -> str:
    "Set or replace a top-level TOML array `key` before the first `[table]` header."
    line = f"{key} = {_fmt_toml_val(values)}"
    m_tbl = re.search(r"(?m)^[ \t]*\[", content)
    preamble, rest = (content[:m_tbl.start()], content[m_tbl.start():]) if m_tbl else (content, "")
    if m := re.search(rf"(?m)^{re.escape(key)}\s*=\s*\[", preamble):
        arr_end = _find_array_end(preamble, m.end()-1)
        preamble = preamble[:m.start()] + line + preamble[arr_end+1:]
    else: preamble = (preamble.rstrip() + "\n" + line + "\n\n").lstrip("\n")
    return preamble + rest

def _sccache_uses_s3() -> bool:
    path = _sccache_config_path()
    if not path.exists(): return False
    try: return "s3" in tomllib.loads(path.read_text()).get("cache", {})
    except tomllib.TOMLDecodeError: return False

def _render_sccache_config(root: Path, spec: dict, content: str) -> str:
    "Return sccache TOML config with `root` in `basedirs`, multilevel `disk`+`s3`, and `[cache.s3]` from `spec`."
    data = tomllib.loads(content) if content else {}
    basedirs = list(data.get("basedirs", []))
    ws_str = str(root.resolve())
    if ws_str not in basedirs: basedirs.append(ws_str)
    content = _set_top_array(content, "basedirs", basedirs)
    if "multilevel" not in data.get("cache", {}):
        content = _replace_table(content, "cache.multilevel", 'chain = ["disk", "s3"]\nwrite_error_policy = "l0"')
    if "disk" not in data.get("cache", {}):
        content = _replace_table(content, "cache.disk", f'dir = {json.dumps(str(_sccache_disk_dir()))}\nsize = 10737418240')
    s3 = dict(data.get("cache", {}).get("s3", {}))
    s3 |= dict(bucket=spec["bucket"], endpoint=spec["endpoint"], region="auto", use_ssl=spec["use_ssl"])
    if spec["key_prefix"]: s3["key_prefix"] = spec["key_prefix"]
    else: s3.pop("key_prefix", None)
    if spec["rw_mode"]: s3["rw_mode"] = spec["rw_mode"]
    elif not spec["access_key"]: s3["rw_mode"] = "READ_ONLY"
    s3["no_credentials"] = not spec["access_key"]
    content = _replace_table(content, "cache.s3", "\n".join(f"{_toml_key(k)} = {_fmt_toml_val(v)}" for k, v in s3.items()))
    return content.strip() + "\n"

def _sccache_r2_needs_setup(root: Path) -> bool:
    url = os.environ.get("SCCACHE_R2", "").strip()
    if not url: return False
    try: spec = _parse_sccache_r2(url)
    except SystemExit: return True
    cfg_path = _sccache_config_path()
    if not cfg_path.exists(): return True
    try:
        original = cfg_path.read_text()
        if _render_sccache_config(root, spec, original) != original: return True
    except tomllib.TOMLDecodeError: return True
    return bool(spec["access_key"]) and not _has_aws_profile()

def _sync_sccache_config(root: Path) -> bool:
    "Configure sccache multilevel local+R2 caching and isolated AWS profile from `SCCACHE_R2`; return True if anything changed."
    url = os.environ.get("SCCACHE_R2", "").strip()
    if not url: return False
    spec = _parse_sccache_r2(url)
    changed = bool(spec["access_key"]) and _sync_aws_profile(spec["access_key"], spec["secret_key"])
    cfg_path = _sccache_config_path()
    original = cfg_path.read_text() if cfg_path.exists() else ""
    new = _render_sccache_config(root, spec, original)
    if new != original:
        tomllib.loads(new)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(new)
        changed = True
    return changed

# The rustc wrapper that `_sync_cargo_wrapper` writes. It imports only `fastws.timelock`, by its file, so that each rustc call pays for little more than the interpreter's start.
_RUSTC_LOCK = r"""#!{python} -IS
# Written by ws-sync. Cargo runs each rustc call through this script, which holds the machine's timing lock shared until rustc ends (`fastws.timelock`){via}.
import os, sys
sys.path.insert(0, {dir!r})
from timelock import exec_shared
INNER = {inner!r}  # The wrapper that each rustc call runs through inside the lock, kept by later syncs; None: rustc alone
PROFILE = {profile!r}  # AWS profile for sccache R2 access when configured by ws-setup; None: leave AWS env alone
if PROFILE:
    os.environ["AWS_PROFILE"] = PROFILE
    for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"): os.environ.pop(_k, None)
exec_shared([INNER, *sys.argv[1:]] if INNER else sys.argv[1:])
"""

def _sync_cargo_wrapper(root: Path, env: dict = None):
    "Point the generated Cargo config's `rustc-wrapper` at `.cargo/rustc-lock`, written here, which runs each rustc call holding the machine's timing lock shared (`fastws.timelock`). Inside the lock it runs the wrapper that the config named before, where that's another tool's, or else sccache where it's installed. On Windows, where the lock does nothing, the config keeps another tool's wrapper, or else names sccache."
    config, script = root/".cargo"/"config.toml", root/".cargo"/"rustc-lock"
    content = config.read_text() if config.exists() else ""
    current = (tomllib.loads(content) if content else {}).get("build", {}).get("rustc-wrapper")
    path_env = (env or os.environ).get("PATH")
    if not (sccache := shutil.which("sccache", path=path_env)):
        print("⚠️  sccache not found; Rust builds won't use compiler caching. Run ws-setup to install it.")
    elif _sccache_r2_needs_setup(root):
        print("⚠️  SCCACHE_R2 is set, but sccache is not configured for it yet. Run ws-setup to configure it.")
    recorded = script.exists() and current == str(script) and re.search(r"(?m)^INNER = (.+?)  #", script.read_text())
    previous = ast.literal_eval(recorded[1]) if recorded else current
    inner = previous if previous not in (None, str(script)) else sccache
    is_sccache = bool(inner) and Path(inner).stem == "sccache"
    profile = _SCCACHE_AWS_PROFILE if (is_sccache and _sccache_uses_s3() and _has_aws_profile()) else None
    if os.name == "nt": wrapper = inner
    else:
        script.parent.mkdir(exist_ok=True)
        rendered = _RUSTC_LOCK.format(python=sys.executable, dir=str(Path(__file__).parent),
            via=f", through {Path(inner).name}" if inner else "", inner=inner, profile=profile)
        if not script.exists() or script.read_text() != rendered:
            script.write_text(rendered)
            script.chmod(0o755)
        wrapper = str(script)
    if not wrapper or current == wrapper: return
    content = re.sub(r"(?m)^rustc-wrapper = .*\n", "", content)
    line = f'rustc-wrapper = {json.dumps(wrapper)}\n'
    if match := re.search(r"(?m)^\[build\][^\n]*\n", content): new = content[:match.end()] + line + content[match.end():]
    else: new = content.rstrip() + ("\n\n" if content.strip() else "") + "[build]\n" + line
    tomllib.loads(new)
    config.parent.mkdir(exist_ok=True)
    config.write_text(new)

def _cargo_dep_tables(data, every=False):
    "A manifest's dependency and build-dependency tables, top-level and per target; with `every`, its dev-dependency tables and `[workspace.dependencies]` too"
    names = ("dependencies", "build-dependencies", "dev-dependencies")[:3 if every else 2]
    for name in names: yield data.get(name, {})
    for target in data.get("target", {}).values():
        for name in names: yield target.get(name, {})
    if every: yield data.get("workspace", {}).get("dependencies", {})

def _rustup_update(root: Path):
    "Update the stable Rust toolchain when the workspace has crates, rustup is installed, and no root toolchain pin exists."
    if not _crate_dirs(root) or not (rustup := shutil.which("rustup")): return
    if any((root/f).exists() for f in ("rust-toolchain.toml", "rust-toolchain")): return
    res = subprocess.run([rustup, "update", "stable"], capture_output=True, text=True)
    if res.returncode: print(f"⚠️  rustup update stable failed\n{((res.stdout or '') + (res.stderr or '')).strip()}")
    elif out := (res.stdout or "").strip(): print(out)

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

def _workspace_patterns(data, path):
    members = data.get("workspaces", [])
    if not isinstance(members, list): raise SystemExit(f"{path}: fastws requires `workspaces` to be a list of package paths.")
    return members

def _yaml_load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict): raise SystemExit(f"{path}: expected a YAML mapping.")
    return data

def _merge_shared(data: dict, shared: dict) -> dict:
    for k,v in shared.items(): data[k] = data[k] | v if isinstance(v, dict) and isinstance(data.get(k), dict) else v
    return data

def _rel_ws_path(root: Path, d: Path) -> str: return Path(os.path.relpath(d.resolve(), root.resolve())).as_posix()

def _maturin_cfg(d: Path) -> dict:
    p = d/'pyproject.toml'
    return tomllib.loads(p.read_text()).get('tool', {}).get('maturin', {}) if p.exists() else {}

def _is_maturin_native(d: Path) -> bool:
    cfg = _maturin_cfg(d)
    return 'python-source' in cfg or cfg.get('bindings') == 'bin'

def _is_bin_with_js(d: Path) -> bool: return (d/'package.json').is_file() and _maturin_cfg(d).get('bindings') == 'bin'

def _repo_roots(root: Path, repos_file="repos.txt") -> list[Path]:
    "Candidate top-level workspace directories plus registered external checkouts."
    exclude = _fastws_cfg(root).get("exclude", [])
    dirs = _project_dirs(root, exclude=exclude)
    dirs += [d for _,d,_ in _load_repo_entries(_resolve_path(root, repos_file), root) if d.is_dir()]
    return list(dict.fromkeys(d.resolve() for d in dirs if not d.name.startswith((".", "_")) and d.name != "node_modules"))

def _wasm_package_spec(d: Path) -> tuple[str, Path] | None:
    "Static `(npm_name, output_dir)` from `[tool.fastws].wasm-package`, or None."
    spec = _fastws_cfg(d).get("wasm-package")
    if spec is None: return None
    if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"] or not isinstance(spec.get("path"), str) or not spec["path"]:
        raise SystemExit(f"{d/'pyproject.toml'}: `[tool.fastws].wasm-package` requires non-empty `name` and `path` strings.")
    return spec["name"], (d/spec["path"]).resolve()

def _wasm_packages(root: Path, repos_file="repos.txt") -> dict:
    "`{repo_dir: (npm_name, pkg_dir)}` for repos declaring `[tool.fastws].wasm-package`."
    return {d: spec for d in _repo_roots(root, repos_file) if (spec := _wasm_package_spec(d))}

def _js_package_specs(d: Path) -> list[Path]:
    "Explicit nested JS package dirs from `[tool.fastws].js-packages`."
    specs = _fastws_cfg(d).get("js-packages", [])
    if not isinstance(specs, list) or not all(isinstance(s, str) and s for s in specs):
        raise SystemExit(f"{d/'pyproject.toml'}: `[tool.fastws].js-packages` must be a list of package paths.")
    return [(d/s).resolve() for s in specs if (d/s/"package.json").is_file()]

def _pnpm_patterns(root: Path) -> list[str]:
    declared = []
    for name in ("pnpm-workspace.yaml", "pnpm-workspace.shared.yaml"):
        path = root/name
        if path.exists() and isinstance(pkgs := _yaml_load(path).get("packages"), list): declared = pkgs
    return declared

def _js_dirs(root: Path, repos_file="repos.txt") -> list[Path]:
    "Find root, nested `[tool.fastws].js-packages`, generated `[tool.fastws].wasm-package` and registered external JS packages."
    exclude = _fastws_cfg(root).get("exclude", [])
    def ok(d):
        return (not d.name.startswith((".", "_")) and d.name != "node_modules"
            and not any(_matches_ws(_rel_ws_path(root, d), e) for e in exclude))
    roots = _project_dirs(root, "package.json", ["*", *_pnpm_patterns(root)])
    repo_roots = _repo_roots(root, repos_file)
    roots += [d for d in repo_roots if (d/"package.json").is_file()]
    res = []
    for d in dict.fromkeys(roots):
        if not ok(d): continue
        data = json.loads((d/"package.json").read_text())
        res += [d] + _project_dirs(d, "package.json", _workspace_patterns(data, d/"package.json"))
    superseded_wasm = set()
    for d in repo_roots:
        if not ok(d) or not (d/"pyproject.toml").is_file(): continue
        res += _js_package_specs(d)
        if spec := _wasm_package_spec(d):
            root_name = json.loads((d/"package.json").read_text()).get("name") if (d/"package.json").is_file() else None
            if root_name == spec[0]: superseded_wasm.add(spec[1])
            else: res.append(spec[1])
    return list(dict.fromkeys(d.resolve() for d in res if ok(d) and d.resolve() not in superseded_wasm))

def _sync_ws_pnpm_workspace(root: Path, members: list[Path], toolchain: dict[str, str] = None) -> tuple[list[str], list[str]]:
    "Generate/update `pnpm-workspace.yaml`, applying Vite+ toolchain overrides and merging `pnpm-workspace.shared.yaml`; return `(added, removed)` packages."
    path = root/"pnpm-workspace.yaml"
    before = path.read_text() if path.exists() else ""
    data = _yaml_load(path) if path.exists() else {}
    cur = list(data.get("packages") or [])
    data.setdefault("linkWorkspacePackages", "deep")
    data.setdefault("preferWorkspacePackages", True)
    data.setdefault("minimumReleaseAge", 1440)
    shared = root/"pnpm-workspace.shared.yaml"
    shared_data = _yaml_load(shared) if shared.exists() else {}
    shared_ov = shared_data.get("overrides") if isinstance(shared_data.get("overrides"), dict) else {}
    if toolchain:
        ov = dict(data.get("overrides") or {})
        if core_v := toolchain.get("vite-plus-core"):
            ov.pop("vite", None)
            if "vite" not in shared_ov: ov["vite@*"] = f"npm:@voidzero-dev/vite-plus-core@{core_v}"
            else: ov.pop("vite@*", None)
        if vitest_v := toolchain.get("vitest"):
            ov.pop("vitest", None)
            if "vitest" not in shared_ov: ov["vitest@*"] = vitest_v
            else: ov.pop("vitest@*", None)
        if ov: data["overrides"] = ov
    if shared_data: _merge_shared(data, shared_data)
    cur_after = list(data.get("packages") or [])
    kept = [e for e in cur_after if any(c in e for c in "*?[") or not (root/e).resolve().is_relative_to(root.resolve())]
    new = kept + [e for e in (_rel_ws_path(root, d) for d in members) if e not in kept]
    data["packages"] = new
    rendered = yaml.safe_dump(data, sort_keys=False)
    if rendered == before: return [], []
    path.write_text(rendered)
    return _entry_changes(cur, new)

def _sync_ws_package_json(root: Path, toolchain: dict[str, str] = None) -> bool:
    "Maintain root `package.json`, applying the global `vite-plus` version and merging optional `package.json.shared`."
    path = root/"package.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    before = json.dumps(data)
    data["private"] = True
    data.pop("workspaces", None)
    if toolchain and (vp_v := toolchain.get("vite-plus")): data["devDependencies"] = {**data.get("devDependencies", {}), "vite-plus": vp_v}
    shared = root/"package.json.shared"
    if shared.exists(): _merge_shared(data, json.loads(shared.read_text()))
    if json.dumps(data) == before: return False
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True

def _js_graph(members: list[Path], wasm_pkgs: dict = None):
    "Package manifests and local build dependencies, keyed by package directory."
    wasm_by_dir = {pkg_dir: name for name, pkg_dir in (wasm_pkgs or {}).values()}
    packages = {}
    for d in members:
        if (d/"package.json").is_file(): packages[d] = json.loads((d/"package.json").read_text())
        elif d in wasm_by_dir: packages[d] = {"name": wasm_by_dir[d]}
    names = {data["name"]: d for d,data in packages.items() if data.get("name")}
    graph = {d: {names[n] for key in ("dependencies", "devDependencies", "optionalDependencies")
        for n in data.get(key, {}) if n in names} for d,data in packages.items()}
    return packages, graph

def _check_rustup(wasm: bool = False):
    "Require rustup's compiler proxies, and optionally the `wasm32-unknown-unknown` target."
    if not (rustup := shutil.which("rustup")):
        raise SystemExit("Rust-backed WebAssembly builds require rustup. Run ws-setup to install it.")
    for tool in "rustc","cargo":
        path = shutil.which(tool)
        if path and os.path.samefile(path, rustup): continue
        raise SystemExit(f"""Rust-backed WebAssembly builds require rustup-managed Rust, but {tool} resolves to {path or 'nothing'}. Put rustup's bin directory first on PATH after Homebrew or other toolchain setup in your shell profile (normally: export PATH="$HOME/.cargo/bin:$PATH"; use $CARGO_HOME/bin when customized). Homebrew Rust can remain installed. Open a new shell and rerun ws-sync.""")
    if wasm:
        installed = subprocess.run([rustup, "target", "list", "--installed"], capture_output=True, text=True, check=True).stdout.splitlines()
        if "wasm32-unknown-unknown" not in installed:
            raise SystemExit("WebAssembly builds require the wasm32-unknown-unknown Rust target. Run ws-setup to install it.")

def _check_vp():
    "Require Vite+ (`vp`) and its managed `node` and `pnpm` shims on PATH."
    if not (vp := shutil.which("vp")): raise SystemExit("JavaScript workspaces require Vite+ (vp). Run ws-setup to install it.")
    for tool in "node", "pnpm":
        path = shutil.which(tool)
        if path and os.path.samefile(path, vp): continue
        raise SystemExit(f"JavaScript workspaces require Vite+-managed {tool}, but {tool} resolves to {path or 'nothing'}. Run ws-setup to enable Vite+ managed shims, and put Vite+'s bin directory first on PATH (normally via `~/.config/vite-plus/env`), then open a new shell and rerun ws-sync.")

def _vp_toolchain() -> dict[str, str]:
    "Versions of `vite-plus`, `vite-plus-core`, and `vitest` from the global `vp` CLI."
    if not shutil.which("vp"): return {}
    raw = subprocess.run(["vp", "toolchain", "--json", "-g"], capture_output=True, text=True, check=True).stdout
    return {n["id"]: n["version"] for n in json.loads(raw).get("nodes", []) if "id" in n and "version" in n}

def _build_wasm(repo: Path):
    "Run `cargo wasm` in `repo` through the workspace's configured Cargo."
    from .cargo import _cargo_command
    cmd, env = _cargo_command("wasm")
    subprocess.run(cmd, cwd=repo, env=env, check=True)

def _run_quiet(cmd: list[str], cwd: Path):
    "Run `cmd`, hiding routine output on success and printing captured output to stderr on failure."
    try: subprocess.run(cmd, check=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError as e:
        if e.stdout: print(e.stdout, file=sys.stderr, end="")
        raise

def _build_js(root: Path, build_workers: int, *, dirs: list[Path] = None):
    "Run workspace JavaScript `build` scripts in topological order via pnpm."
    if dirs is not None:
        if not dirs: return
        filters = [arg for d in dirs for arg in ("--filter", f"{{{_rel_ws_path(root, d)}}}...")]
        cmd = ["pnpm", *filters, "--if-present", f"--workspace-concurrency={build_workers}", "run", "build"]
    else: cmd = ["pnpm", "-r", "--if-present", f"--workspace-concurrency={build_workers}", "run", "build"]
    _run_quiet(cmd, root)

def _sync_js(root: Path, build_workers: int):
    "Install the workspace with Vite+ and run topological JavaScript builds."
    _run_quiet(["vp", "install", "--silent"], root)
    _build_js(root, build_workers)

@call_parse
async def ws_sync(
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update from local git remotes
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
    workers: int = 64,  # Number of parallel clone, pull and dependency-update workers
    upgrade: bool = False,  # Force the once-daily dependency upgrade pass
    build_workers: int = os.cpu_count() or 1,  # Concurrent projects per build phase; defaults to CPU count
):
    "Pull the workspace baseline, clone missing shared/local repos, then pull projects and sync; upgrade Python deps daily and Rust toolchain/Cargo deps weekly."
    with _cli_errors():
        root = _ws_root(workspace, repos_file, pyproject_file, template_file)
        repos_path = _resolve_path(root, repos_file)
        pyproject_path = _resolve_path(root, pyproject_file)
        template_path = _resolve_path(root, template_file)
        await _pull_workspace(root)
        entries = _load_repo_entries(repos_path, root)
        await _clone_repos(entries, workers)
        repos = await _discover_ws_repos(root)
    
        if missing_repos := _update_repos_file(repos_path, repos): print(f"Added local repos: {', '.join(missing_repos)}")
        entries = _load_repo_entries(repos_path, root)
        ext_dirs = [d for _,d,_ in entries if d.resolve().parent != root.resolve()]
        dirs = [d for _,d,_ in entries]
        try: dirs = await _changed_dirs(dirs)
        except Exception: pass
        await _pull(dirs, workers=workers)
    
        if not pyproject_path.exists(): _sync_ws_pyproject(pyproject_path, template_path, [])
        added_ex, removed_ex = _sync_ws_excludes(pyproject_path, root, {d.name for _,d,_ in entries if d.resolve().parent == root.resolve()})
        if added_ex: print(f"Auto-excluded from the workspace: {', '.join(added_ex)}")
        if removed_ex: print(f"No longer excluded from the workspace: {', '.join(removed_ex)}")
    
        missing_projects = _sync_ws_pyproject(pyproject_path, template_path, _ws_projects(root), _external_projects(root, ext_dirs),
            _repo_extras(root, entries))
        if missing_projects: print(f"Added workspace projects: {', '.join(missing_projects)}")
        projects = _build_projects(root, repos_file)
        _sync_build_backend(pyproject_path, projects)
    
        added_p, removed_p = cargo_setup(root)
        if added_p: print(f"Cargo patches added: {', '.join(added_p)}")
        if removed_p: print(f"Cargo patches removed: {', '.join(removed_p)}")
    
        wasm_pkgs = _wasm_packages(root, repos_file)
        js_members = _js_dirs(root, repos_file)
        if wasm_pkgs: _check_rustup(wasm=True)
        if js_members: _check_vp()
        if js_members or any((root/f).exists() for f in ("pnpm-workspace.yaml", "pnpm-workspace.shared.yaml", "package.json.shared")):
            tc = _vp_toolchain() if js_members else {}
            added_j, removed_j = _sync_ws_pnpm_workspace(root, js_members, toolchain=tc)
            _sync_ws_package_json(root, toolchain=tc)
            if added_j: print(f"JS workspace packages added: {', '.join(added_j)}")
            if removed_j: print(f"JS workspace packages removed: {', '.join(removed_j)}")

        if bad := _pending_dirs(root):
            print(f"⚠️  Skipping uv sync, not Python projects yet (scaffold with e.g. nbdev-new or ship-new, or remove): {', '.join(bad)}")
            return
        up = upgrade or _should_upgrade(root)
        cargo_up = upgrade or _should_upgrade(root, max_age=7*86400, name="fastws-cargo-upgraded")
        if cargo_up:
            _rustup_update(root)
            _cargo_update(root, workers=workers)
        subprocess.run(["uv", "sync", "-U"] if up else ["uv", "sync"], check=True, cwd=root)
        _write_pytest_pth(root)
        from .cargo import develop
        native = [d.resolve() for _,d in projects if _is_maturin_native(d)]
        post_js = [d for d in native if _is_bin_with_js(d)]
        pre_js = [d for d in native if d not in post_js]
        if pre_js:
            with ThreadPoolExecutor(max_workers=build_workers) as ex: list(ex.map(develop, pre_js))
        if wasm_pkgs:
            with ThreadPoolExecutor(max_workers=build_workers) as ex: list(ex.map(_build_wasm, wasm_pkgs))
        if up: _upgrade_stamp(root).touch()
        if cargo_up: _upgrade_stamp(root, "fastws-cargo-upgraded").touch()
        if js_members: _sync_js(root, build_workers)
        if post_js:
            with ThreadPoolExecutor(max_workers=build_workers) as ex: list(ex.map(develop, post_js))

@call_parse
async def ws_add(
    repo: str,  # Repo to add (owner/repo to clone), or an existing local folder (a path outside the workspace stays where it is)
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
    workers: int = 64,  # Number of parallel workers
):
    "Add a personal repo to the local list (a local folder resolves via its origin remote), then run ws-sync."
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
    if not any((d/f).exists() for f in ("pyproject.toml", "Cargo.toml", "package.json")):
        raise SystemExit(f"{d.name} has no pyproject.toml, Cargo.toml or package.json: scaffold the project first, then re-run ws-add")
    return parsed, True, None

def _resolve_removal_target(root: Path, repo: str, repos_path: Path) -> str:
    "Canonical owner/repo for `repo`, matching an existing folder name when `repo` isn't a valid spec."
    if _is_repo_spec(repo): return _normalize_repo(repo)
    if not (root/repo).is_dir(): return _normalize_repo(repo)  # invalid spec and no such folder: raise
    for r,d,_ in _load_repo_entries(repos_path, root):
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
    repo: str,  # Repo to remove, e.g. owner/repo or a local folder name
    *repos: str,  # Additional repos to remove
    workspace: str = "",  # Workspace root; defaults to active venv parent when available
    repos_file: str = "repos.txt",  # Repo list to update
    pyproject_file: str = "pyproject.toml",  # Workspace pyproject to update
    template_file: str = "pyproject.tmpl",  # Template copied when pyproject.toml is missing
):
    "Remove personal repos and their local metadata; shared baseline members cannot be removed."
    root = _ws_root(workspace, repos_file, pyproject_file, template_file)
    repos_path = _resolve_path(root, repos_file)
    pyproject_path = _resolve_path(root, pyproject_file)
    local_path = _local_repos_path(repos_path)
    baseline = {_repo_key(r) for r in _load_repos(repos_path)}
    local = {_repo_key(r) for r in _load_repos(local_path)}
    entries = _load_repo_entries(repos_path, root)
    targets = {}
    for repo in (repo, *repos):
        repo = _resolve_removal_target(root, repo, repos_path)
        key = _repo_key(repo)
        if key in baseline: raise SystemExit(f'{repo} belongs to the shared baseline: edit {repos_path} deliberately to remove it')
        d = _resolve_repo_dir(root, repo)
        for r,location,_ in entries:
            if _repo_key(r) == key and location.resolve() != d:
                raise SystemExit(f'Refusing to remove {repo}: custom checkout location {location}; manage it explicitly')
        if d in targets: continue
        if not d.exists() and key not in local: raise SystemExit(f"Nothing to remove for {repo}")
        if d.exists() and (issues := _repo_safety_issues(d)):
            raise SystemExit("Refusing to remove:\n" + "\n".join(f"  - {i}" for i in issues))
        name = _read_pyproject_name(d/"pyproject.toml") if (d/"pyproject.toml").exists() else None
        targets[d] = repo, name or d.name
    for d,(repo,name) in targets.items():
        _remove_from_repos_file(local_path, repo)
        _remove_from_pyproject(pyproject_path, [name])
        if d.exists():
            try: ans = input(f"Remove directory {d}? [y/N] ")
            except EOFError: ans = ""
            if ans.strip().lower() in ("y", "yes"): shutil.rmtree(d)
    subprocess.run(["uv", "sync"], check=True, cwd=root)
