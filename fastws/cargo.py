r"Build and install Cargo artifacts for mixed Rust/Python projects."
import fcntl, json, os, shutil, subprocess, sys, sysconfig, time
from contextlib import contextmanager
from pathlib import Path

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib

# The file whose lock keeps timings and builds apart on this machine. Timings take it exclusively. Builds and test runs take it shared.
_TIMING_LOCK = '/tmp/aai-ws-timing.lock'


class ConfigError(ValueError):
    "A project's `pyproject.toml` lacks a setting that a command needs."


def settings(root):
    "Read the project's tool configuration."
    return tomllib.loads((root/'pyproject.toml').read_text()).get('tool', {})


def cargo(root, *args):
    "Run Cargo in the project and return its standard output."
    return subprocess.run(['cargo', *args], cwd=root, stdout=subprocess.PIPE, text=True, check=True).stdout


def members(root):
    "The manifests of the workspace's member packages."
    return {Path(p['manifest_path']) for p in json.loads(cargo(root, 'metadata', '--no-deps', '--format-version', '1'))['packages']}


def _lock_users():
    "The other processes that have the timing lock's file open, as `pid command` strings from `lsof`. On macOS, where `lsof` doesn't show `flock` holds, the list also includes any process waiting for the lock."
    users, pid = [], ''
    for line in subprocess.run(['lsof', '+c', '0', '-F', 'pc', _TIMING_LOCK], capture_output=True, text=True).stdout.splitlines():
        if line[:1] == 'p': pid = line[1:]
        elif line[:1] == 'c' and pid != str(os.getpid()): users.append(f'{pid} {line[1:]}')
    return users


@contextmanager
def timing_lock(
    exclusive=False, # Take the lock exclusively, as a timing does; False: take it shared, as a build or a test run does
):
    "Hold the lock that keeps this machine's timings apart from its builds and test runs. A hold waits up to 5 minutes for the lock. After that, a shared hold goes ahead without it. An exclusive hold raises `TimeoutError`. An exclusive hold that has waited 30 seconds prints, once, the other processes that have the lock's file open."
    with open(_TIMING_LOCK, 'a') as f:
        start, waiting, reported = time.monotonic(), False, False
        while True:
            try:
                fcntl.flock(f, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError: pass
            waited = time.monotonic() - start
            if waited > 300:
                if exclusive: raise TimeoutError(f'a build or another timing has held {_TIMING_LOCK} for 5 minutes')
                print(f'going ahead although a timing still holds {_TIMING_LOCK}', file=sys.stderr)
                break
            if not waiting: print(f'waiting for {"a build or another timing" if exclusive else "a timing"} to release {_TIMING_LOCK}', file=sys.stderr)
            waiting = True
            if exclusive and not reported and waited > 30:
                print(f'{_TIMING_LOCK} is still taken after 30 seconds. Processes with it open: {", ".join(_lock_users()) or "none"}', file=sys.stderr)
                reported = True
            time.sleep(0.25)
        yield


def check(root):
    "Check every target of the workspace with Cargo, holding the timing lock shared. Cargo's messages are in the result's `stderr`."
    with timing_lock(): return subprocess.run(['cargo', 'check', '--all-targets', '--message-format=short'], cwd=root, stderr=subprocess.PIPE, text=True)


def sources(root):
    "The manifests, and the target directories or files, of the local packages that the workspace builds: its members and its path dependencies."
    res = set()
    for p in json.loads(cargo(root, 'metadata', '--format-version', '1'))['packages']:
        if p['source'] is not None: continue
        manifest = Path(p['manifest_path'])
        res.add(manifest)
        for t in p['targets']:
            src = Path(t['src_path'])
            res.add(src if src.parent == manifest.parent else src.parent)
    return res


def build(root, *, library=True, binaries=False, profile=None, target=None, no_default_features=False):
    "Build with Cargo and return the compiler artifacts of the workspace's packages."
    cfg = settings(root)['maturin']
    cmd = ['build', '--manifest-path', str((root/'Cargo.toml').resolve()), '--message-format=json-diagnostic-short,json-render-diagnostics']
    if library: cmd.append('--lib')
    if binaries: cmd.append('--bins')
    if profile := profile or cfg.get('profile'): cmd += ['--profile', profile]
    if target: cmd += ['--target', target]
    if no_default_features: cmd.append('--no-default-features')
    else:
        if features := cfg.get('features'): cmd += ['--features', ','.join(features)]
        for flag in ('all-features', 'no-default-features'):
            if cfg.get(flag): cmd.append('--' + flag)
    with timing_lock(): messages = [json.loads(line) for line in cargo(root, *cmd).splitlines()]
    workspace = members(root)
    return [m for m in messages if m.get('reason') == 'compiler-artifact' and Path(m['manifest_path']) in workspace]


def replace(source, destination):
    "Replace an installed artifact without changing the file held by running processes."
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + '.tmp')
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)
    return destination


def develop(root):
    "Install a Python extension, opted-in binaries and wheel data files from one Cargo build."
    tool = settings(root)
    cfg = tool['maturin']
    if 'python-source' not in cfg: raise ConfigError(r'cargo develop requires tool.maturin.python-source')
    binaries = tool.get('fastws', {}).get('native-binaries', False)
    artifacts = build(root, binaries=binaries, profile='test')
    artifact, = [m for m in artifacts if 'cdylib' in m['target']['crate_types']]
    lib, = [f for f in artifact['filenames'] if Path(f).suffix in ('.dylib', '.so', '.dll')]
    module = cfg.get('module-name', artifact['target']['name'])
    dest = root/cfg['python-source']/Path(*module.split('.'))
    replace(lib, dest.with_name(dest.name + sysconfig.get_config_var('EXT_SUFFIX')))
    env = Path(os.environ.get('VIRTUAL_ENV', sys.prefix))
    if binaries:
        scripts = env/('Scripts' if os.name == 'nt' else 'bin')
        for m in artifacts:
            if m.get('executable'): replace(m['executable'], scripts/Path(m['executable']).name)
    # Editable builds through `build_backend` don't install the wheel's data files, which a wheel install puts under the prefix.
    if 'data' in cfg:
        data = root/cfg['data']/'data'
        for f in data.rglob('*'):
            if f.is_file(): replace(f, env/f.relative_to(data))


def stage_binaries(root, profile=None, target=None):
    "Stage Python-free Cargo binaries in maturin's wheel-data directory."
    cfg = settings(root)['maturin']
    if 'data' not in cfg: raise ConfigError(r'staging native binaries requires tool.maturin.data')
    dest = root/cfg['data']/'scripts'
    artifacts = build(root, library=False, binaries=True, profile=profile, target=target, no_default_features=True)
    if dest.exists(): shutil.rmtree(dest)
    return [replace(m['executable'], dest/Path(m['executable']).name) for m in artifacts if m.get('executable')]
