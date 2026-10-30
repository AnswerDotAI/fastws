r"""Builds that `cargo develop` installs, each kept once, read-only, and named by the hash of its inputs and files. A project's installed build is a pointer to one of them, which installing replaces in one step, so a process or a timing that resolves a build never sees it change.

A build is a directory under `STORE/<project>/`, named by its id, that holds `build.json` and a hard link to a blob for each file: `python/…` for the Python package with its extension, `bin/…` for binaries and `data/…` for wheel data. Each blob, under `STORE/blobs/`, is named by its contents' sha256, so files that don't change between builds take no more space. Installed paths are symbolic links through `STORE/<project>/installed`, a link to the installed build's directory. Windows installs copies."""
import hashlib, json, os, re, shutil, time, tomllib
from datetime import datetime
from pathlib import Path

STORE = Path(os.environ.get('XDG_CACHE_HOME') or '~/.cache').expanduser()/'fastws'/'builds' # Each project's builds, and the blobs that their files link to
KEEP, KEEP_DAYS = 20, 2 # `collect` keeps each project's installed build, its last `KEEP` builds and its builds of the last `KEEP_DAYS` days


def _project(root): return STORE/tomllib.loads((Path(root)/'pyproject.toml').read_text())['project']['name']

def _digest(path):
    with open(path, 'rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

def _blob(path):
    "The blob that holds `path`'s contents, read-only and executable where `path` is, added to the store where it's new."
    blob = STORE/'blobs'/_digest(path)
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_name(f'{blob.name}.{os.getpid()}.tmp')
        shutil.copy2(path, tmp)
        tmp.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
        os.replace(tmp, blob)
    return blob


def dep_files(artifact):
    "The files that Cargo's dep-info lists for `artifact`, the `.d` file beside it, with the files under each directory it lists, such as one that a build script watches."
    deps = re.split(r'(?<!\\)\s+', Path(artifact).with_suffix('.d').read_text().split(': ', 1)[1].strip())
    for p in (Path(d.replace('\\ ', ' ')) for d in deps):
        yield from (sorted(f for f in p.rglob('*') if f.is_file()) if p.is_dir() else [p])


def _kept(d):
    "The builds in project directory `d`, leaving out its `installed` link and builds still being written."
    return [p for p in d.iterdir() if not p.is_symlink() and not p.name.endswith('.tmp') and (p/'build.json').exists()] if d.exists() else []


def keep(
    root, # The project directory, whose `pyproject.toml` names the project
    files:dict, # Each path in the build, such as `bin/bpl`, to the file to keep there
    inputs, # The files the build was made from
)->Path:
    "Keep a build of the project at `root`, giving its directory. Its id hashes the contents of `inputs` and of `files`, so a build kept before is found again rather than written twice, and builds whose files differ never share an id."
    sources = {str(p): _digest(p) for p in inputs}
    blobs = {rel: _blob(src) for rel, src in files.items()}
    key = json.dumps([sorted(sources.items()), sorted((rel, b.name) for rel, b in blobs.items())], sort_keys=True)
    build = _project(root)/hashlib.sha256(key.encode()).hexdigest()[:16]
    if build.exists(): return build
    tmp = build.with_name(f'{build.name}.{os.getpid()}.tmp')
    for rel, blob in blobs.items():
        (tmp/rel).parent.mkdir(parents=True, exist_ok=True)
        os.link(blob, tmp/rel)
    (tmp/'build.json').write_text(json.dumps(dict(id=build.name, time=time.time(), author=os.environ.get('EXHASH_AUTHOR'), inputs=sources), indent=1))
    try: os.replace(tmp, build)
    except OSError: shutil.rmtree(tmp) # Another process kept the same build first
    return build


def _link(path, target):
    "Make `path` a symbolic link to `target`, replacing what was there in one step."
    if path.is_symlink() and os.readlink(path) == str(target): return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, path)


def install(
    root, # The project directory
    build:Path, # A build that `keep` gave
    paths:dict, # Each installed path, such as the extension in the project's Python package, to its path in the build
):
    "Make `build` the installed build of the project at `root`. Each of `paths` becomes a link through the project's `installed` link, which then points at `build`. Windows copies the files instead."
    installed = _project(root)/'installed'
    for path, rel in paths.items():
        if os.name == 'nt': shutil.copy2(build/rel, path)
        else: _link(Path(path), installed/rel)
    if os.name != 'nt': _link(installed, build.name)


def resolve(
    root, # The project directory
    build:str=None, # A prefix of a kept build's id; None: the installed build
)->Path:
    "The directory of the build of the project at `root` that `build` names, as it is now."
    d = _project(root)
    if build is None: return (d/'installed').resolve()
    found = [p for p in _kept(d) if p.name.startswith(build)]
    if len(found) != 1: raise ValueError(f'{len(found)} builds of {d.name} have ids starting {build!r}')
    return found[0]


def builds(
    root, # The project directory
    n:int=10, # How many of the newest builds to give
)->str:
    "The newest `n` builds of the project at `root`, one line each: its id, when it was kept, who kept it where known, and the input files that changed since the build before. `*` marks the installed build."
    d = _project(root)
    installed = (d/'installed').resolve().name if (d/'installed').exists() else None
    metas = sorted((json.loads((p/'build.json').read_text()) for p in _kept(d)), key=lambda m: m['time'])
    lines, before = [], {}
    for m in metas:
        changed = sorted(Path(f).name for f, h in m['inputs'].items() if before.get(f) != h) if before else ['(first)']
        lines.append(f"{'*' if m['id'] == installed else ' '} {m['id']}  {datetime.fromtimestamp(m['time']):%m-%d %H:%M}  {m['author'] or '-':<8} {', '.join(changed[:8]) or '(no input changed)'}{' …' if len(changed) > 8 else ''}")
        before = m['inputs']
    return '\n'.join(lines[-n:])


def collect():
    "Delete each project's builds that are neither installed, among its last `KEEP`, nor from the last `KEEP_DAYS` days, then each blob that no build links to and that's at least an hour old."
    now = time.time()
    for d in (p for p in STORE.iterdir() if p.is_dir() and p.name != 'blobs') if STORE.exists() else []:
        installed = (d/'installed').resolve() if (d/'installed').exists() else None
        kept = sorted(_kept(d), key=lambda p: (p/'build.json').stat().st_mtime)
        for p in kept[:-KEEP]:
            if p != installed and now - (p/'build.json').stat().st_mtime > KEEP_DAYS*86400: shutil.rmtree(p)
    for blob in (STORE/'blobs').iterdir() if (STORE/'blobs').exists() else []:
        st = blob.stat()
        if st.st_nlink == 1 and now - st.st_mtime > 3600: blob.unlink()
