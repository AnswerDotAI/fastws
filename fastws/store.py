r"""Builds that `cargo develop` installs, each kept once, read-only, and named by the hash of its inputs and files. A project's installed build is a pointer to one of them, which installing replaces in one step, so a process or a timing that resolves a build never sees it change.

A build is a directory under `STORE/<project>/`, named by its id, that holds `build.json` and a hard link to a blob for each file: `python/…` for the Python package with its extension, `bin/…` for binaries and `data/…` for wheel data. Each blob, under `STORE/blobs/`, is named by its contents' sha256, so files that don't change between builds take no more space. Installed paths are symbolic links through `STORE/<project>/installed`, a link to the installed build's directory. Windows installs copies."""
import hashlib, json, os, re, shutil, time
if os.name != 'nt': import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from ._toml import tomllib

STORE = Path(os.environ.get('XDG_CACHE_HOME') or '~/.cache').expanduser()/'fastws'/'builds' # Each project's builds, and the blobs that their files link to
KEEP, KEEP_DAYS = 20, 2 # `collect` keeps each project's installed build, its last `KEEP` builds and its builds of the last `KEEP_DAYS` days


def _project(root): return STORE/tomllib.loads((Path(root)/'pyproject.toml').read_text())['project']['name']

@contextmanager
def building(root):
    "On Unix, serialize cooperating `cargo develop` runs through build, capture and installation; yield the build's start time. Other Cargo commands do not take this lock."
    project = _project(root)
    project.mkdir(parents=True, exist_ok=True)
    with open(project/'build.lock', 'a') as lock:
        if os.name != 'nt': fcntl.flock(lock, fcntl.LOCK_EX)
        yield time.time()

@contextmanager
def access(exclusive=False):
    "On Unix, keep collection apart from readers and capture/install. Hold this shared across `keep` and `install`; `collect` takes it exclusively."
    STORE.mkdir(parents=True, exist_ok=True)
    with open(STORE/'store.lock', 'a') as lock:
        if os.name != 'nt': fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield

def _digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1024*1024): digest.update(chunk)
    return digest.hexdigest()

def _blob(path):
    "The blob that holds `path`'s contents, read-only and executable where `path` is, added to the store where it's new."
    directory = STORE/'blobs'
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=directory, suffix='.tmp') as tmp:
        captured = Path(tmp)/'file'
        shutil.copy2(path, captured)
        blob = directory/_digest(captured)
        captured.chmod(0o555 if captured.stat().st_mode & 0o111 else 0o444)
        try: os.link(captured, blob)
        except FileExistsError: pass
    return blob


def dep_files(artifact):
    "The files that Cargo's dep-info lists for `artifact`, the `.d` file beside it, with the files under each directory it lists, such as one that a build script watches."
    deps = re.split(r'(?<!\\)\s+', Path(artifact).with_suffix('.d').read_text().split(': ', 1)[1].strip())
    for p in (Path(d.replace('\\ ', ' ')) for d in deps):
        yield from (sorted(f for f in p.rglob('*') if f.is_file()) if p.is_dir() else [p])


def _kept(d):
    "The builds in project directory `d`, leaving out its `installed` link and builds still being written."
    return [p for p in d.iterdir() if not p.is_symlink() and not p.name.endswith('.tmp') and (p/'build.json').exists()] if d.exists() else []


def _metas(d):
    "The `build.json` records of the builds in project directory `d`, oldest first."
    return sorted((json.loads((p/'build.json').read_text()) for p in _kept(d)), key=lambda m: m['time'])


def keep(
    root, # The project directory, whose `pyproject.toml` names the project
    files:dict, # Each path in the build, such as `bin/bpl`, to the file to keep there
    inputs, # The files the build was made from
    started:float, # The build's start time, as `building` gives it
)->Path:
    "Keep captured artifacts and observed input digests. Hold `building` and `access` through capture/install. `edited` flags inputs possibly modified since build start, by mtime, not compiler provenance. Identical inputs and artifacts reuse their first capture's record."
    sources = {str(p): _digest(p) for p in inputs}
    blobs = {rel: _blob(src) for rel, src in files.items()}
    key = json.dumps([sorted(sources.items()), sorted((rel, b.name) for rel, b in blobs.items())], sort_keys=True)
    build = _project(root)/hashlib.sha256(key.encode()).hexdigest()[:16]
    if build.exists(): return build
    with TemporaryDirectory(dir=build.parent, suffix='.tmp') as tmp:
        tmp = Path(tmp)
        for rel, blob in blobs.items():
            (tmp/rel).parent.mkdir(parents=True, exist_ok=True)
            os.link(blob, tmp/rel)
        edited = sorted(f for f in sources if Path(f).stat().st_mtime >= started)
        (tmp/'build.json').write_text(json.dumps(dict(id=build.name, time=time.time(), started=started, author=os.environ.get('EXHASH_AUTHOR'), inputs=sources, edited=edited), indent=1))
        os.replace(tmp, build)
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
    "Install a captured build while holding `access`. Each path links through the project's `installed` link, which then points at `build`. Windows copies the files instead."
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
    with access():
        d = _project(root)
        if build is None: return (d/'installed').resolve()
        found = [p for p in _kept(d) if p.name.startswith(build)]
        if len(found) != 1: raise ValueError(f'{len(found)} builds of {d.name} have ids starting {build!r}')
        return found[0]


class Builds(list):
    "A project's first capture of each build, oldest first: `id`, `time`, `started`, `author`, capture-time `inputs` digests, `edited` and `changes`. `edited` flags inputs possibly modified since build start, by mtime, not what the compiler read. `changes` lists inputs whose digest differs from the build before's, or None for the first. `installed` is the installed build's id."
    def __init__(self, rows=(), installed=None):
        super().__init__(rows)
        self.installed = installed
    def __repr__(self): return '\n'.join(map(self._line, self))
    def __getitem__(self, i): return Builds(super().__getitem__(i), self.installed) if isinstance(i, slice) else super().__getitem__(i)
    def _line(self, m):
        names = ['(first)'] if m['changes'] is None else sorted(Path(f).name for f in m['changes'])
        edited = f"  possibly modified since build start: {', '.join(sorted(Path(f).name for f in m['edited']))}" if m['edited'] else ''
        return f"{'*' if m['id'] == self.installed else ' '} {m['id']}  {datetime.fromtimestamp(m['time']):%m-%d %H:%M:%S}  {m['author'] or '-':<8} {', '.join(names[:8]) or '(no input changed)'}{' …' if len(names) > 8 else ''}{edited}"


def builds(
    root, # The project directory
    n:int=10, # How many of the newest builds to give; None: every kept build
)->Builds:
    "The newest `n` builds, oldest first, with their input changes and diagnostic mtime flags. `*` marks the installed build."
    with access():
        d = _project(root)
        metas = _metas(d)
        for prev, m in zip([None, *metas], metas):
            m['changes'] = None if prev is None else sorted(f for f, h in m['inputs'].items() if prev['inputs'].get(f) != h)
            m.setdefault('edited', [])
        return Builds(metas[-n:] if n else metas, (d/'installed').resolve().name if (d/'installed').exists() else None)


def collect():
    "Delete builds neither installed, among the last `KEEP`, nor from the last `KEEP_DAYS` days, then unlinked blobs at least an hour old. Exclusive store access keeps this apart from readers and capture/install operations. Call outside `access`."
    if not STORE.exists(): return
    with access(exclusive=True):
        now = time.time()
        for d in (p for p in STORE.iterdir() if p.is_dir() and p.name != 'blobs'):
            installed = (d/'installed').resolve() if (d/'installed').exists() else None
            kept = sorted(_kept(d), key=lambda p: (p/'build.json').stat().st_mtime)
            for p in kept[:-KEEP]:
                if p != installed and now - (p/'build.json').stat().st_mtime > KEEP_DAYS*86400: shutil.rmtree(p)
        for blob in (STORE/'blobs').iterdir() if (STORE/'blobs').exists() else []:
            st = blob.stat()
            if st.st_nlink == 1 and now - st.st_mtime > 3600: blob.unlink()
