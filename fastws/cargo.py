r"Build, test and install Cargo artifacts for mixed Rust/Python projects."
import fcntl, json, os, re, shutil, subprocess, sys, sysconfig, time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib

# The file whose lock keeps timings and builds apart on this machine. Timings take it exclusively. Builds and test runs take it shared.
_TIMING_LOCK = '/tmp/aai-ws-timing.lock'


class ConfigError(ValueError):
    "A project's `pyproject.toml` lacks a setting that a command needs."


def settings(root):
    "Read the project's tool configuration."
    return tomllib.loads((Path(root).expanduser()/'pyproject.toml').read_text()).get('tool', {})


def cargo(root, *args):
    "Run Cargo in the project and return its standard output."
    return subprocess.run(['cargo', *args], cwd=Path(root).expanduser(), stdout=subprocess.PIPE, text=True, check=True).stdout


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
    "Check every target of the workspace with Cargo, holding the timing lock shared. Cargo's messages are in the result's `stderr`, which `diagnostics` reads."
    with timing_lock(): return subprocess.run(['cargo', 'check', '--all-targets', '--message-format=short'], cwd=Path(root).expanduser(), stderr=subprocess.PIPE, text=True)


# Cargo's line that starts a test binary: `Running` with its target and executable, or `Doc-tests` with its package.
_SUITE = re.compile(r'^\s+(?:Running (.+) \([^)]*\)|(Doc-tests .+))$')
_TEST = re.compile(r'^test (.+) \.\.\. (ok|FAILED|ignored)')
_FAILURE = re.compile(r'^---- (.+) std(?:out|err) ----$')
_DIAGNOSTIC = re.compile(r'^(\S.*?):(\d+):(\d+): (warning|error)(?:\[\w+\])?: (.*)$')


class Diagnostic(NamedTuple):
    "A compiler message, read from Cargo's short format."
    path:str; line:int; col:int; level:str; message:str
    def __str__(self): return f'{self.level}: {self.path}:{self.line}: {self.message}'


def diagnostics(text):
    "Each `Diagnostic` in `text`, which holds Cargo's short-format messages, such as `check`'s `stderr`."
    return [Diagnostic(m[1], int(m[2]), int(m[3]), m[4], m[5]) for l in text.splitlines() if (m := _DIAGNOSTIC.match(l))]


def _lines(text, n=20, tail=False):
    "Up to `n` lines of `text`, indented, from its end if `tail`, with a count of the lines left out."
    lines = text.splitlines()
    shown = [f'    {l}' for l in (lines[-n:] if tail else lines[:n])]
    if len(lines) <= n: return shown
    more = f'    … {len(lines)-n} more lines'
    return [more, *shown] if tail else [*shown, more]


@dataclass
class Suite:
    "What one test binary printed when `cargo test` ran it."
    name:str # Cargo's name for the binary, such as `tests/core.rs`, `unittests src/lib.rs` or `Doc-tests basedpl`
    output:str # Everything printed after Cargo's line that starts the binary, until the next binary starts
    tests:dict # Each test's outcome: `ok`, `FAILED` or `ignored`
    failures:dict # Each failing test's captured output
    summary:str # libtest's `test result:` line, empty for a binary that crashed or has its own harness
    failed:bool # Did Cargo report the binary as failing?

    @classmethod
    def read(cls, name, lines):
        "Read one binary's lines."
        failures, current = {}, None
        for line in lines:
            if m := _FAILURE.match(line): current = failures.setdefault(m[1], [])
            elif line == 'failures:': current = None
            elif current is not None: current.append(line)
        return cls(name, '\n'.join(lines), {m[1]: m[2] for l in lines if (m := _TEST.match(l))},
                   {k: '\n'.join(v).strip('\n') for k,v in failures.items()},
                   next((l for l in lines if l.startswith('test result: ')), ''), any(l.startswith('error: test failed') for l in lines))

    def __str__(self):
        n = Counter(self.tests.values())
        status = ', '.join(f'{n[o]} {w}' for o,w in [('ok', 'passed'), ('FAILED', 'failed'), ('ignored', 'ignored')] if n[o])
        if self.failed and not n['FAILED']: status = f'{status}, then failed' if status else 'failed'
        time = f' in {m[1]}' if (m := re.search(r'finished in (\S+)$', self.summary)) else ''
        return f'{self.name}: {status or "ok"}{time}'


@dataclass
class Results:
    "What `cargo test` printed, read into the build's compiler messages and each test binary's results. It displays as a summary: the compiler messages, each binary that ran tests, and each failure's output."
    returncode:int
    text:str # Everything Cargo and the tests printed, standard output and standard error in the order they were written
    diagnostics:list # Each `Diagnostic` from the build, before the first binary ran
    suites:list # Each binary's `Suite`, in the order Cargo ran them

    @property
    def ok(self): return self.returncode == 0

    def __repr__(self):
        res = ['cargo test passed' if self.ok else f'cargo test failed with exit status {self.returncode}']
        diags = sorted(self.diagnostics, key=lambda d: d.level != 'error')
        res += [str(d) for d in diags[:20]]
        if len(diags) > 20: res.append(f'{len(diags)-20} more compiler messages in `diagnostics`')
        ran = [s for s in self.suites if s.tests or not s.summary]
        for s in ran:
            res.append(str(s))
            for t,out in s.failures.items(): res += [f'  FAILED {t}', *_lines(out)]
            if s.failed and not s.failures: res += _lines(s.output, tail=True)
        if idle := len(self.suites) - len(ran): res.append(f'{idle} test binaries ran no tests')
        if not (self.ok or self.suites or diags): res += _lines(self.text, tail=True)
        return '\n'.join(res)


def _blocks(text):
    "Split `text` at Cargo's lines that start a test binary, into `(name, lines)` pairs. The first pair, named `None`, holds the build's lines."
    res = [(None, [])]
    for line in text.splitlines():
        if m := _SUITE.match(line): res.append((m[1] or m[2], []))
        else: res[-1][1].append(line)
    return res


def run_tests(
    root, # A Cargo package or workspace (expands `~`)
    *args, # Arguments for `cargo test`, such as `'--test', 'core'`, a test name to filter by, or `'--', '--nocapture'`
    env:dict=None, # Environment variables to set for the run, such as `{'BASEDPL_COSTS': '1'}`
    timeout:float=None # Seconds before the run stops, raising `subprocess.TimeoutExpired`
)->Results:
    "Run `cargo test` in `root` and read what it prints. Compiler messages come in Cargo's short format. It doesn't hold the timing lock as `check` and `build` do: tests that time take the lock exclusively themselves, and a shared hold here would block them."
    # A quiet Cargo config hides the `Running` lines that divide the binaries' output.
    p = subprocess.run(['cargo', 'test', '--message-format=short', *args], cwd=Path(root).expanduser(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                       env=os.environ | {'CARGO_TERM_QUIET': 'false', 'CARGO_TERM_COLOR': 'never'} | (env or {}))
    (_, build), *runs = _blocks(p.stdout)
    return Results(p.returncode, p.stdout, diagnostics('\n'.join(build)), [Suite.read(*b) for b in runs])


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


def _maturin_flags(target, env):
    "Maturin's macOS linker flags without replacing existing compiler flags."
    target = target or env.get('CARGO_BUILD_TARGET')
    if not (target.endswith('-apple-darwin') if target else sys.platform == 'darwin'): return [], None
    link_args = ['link-arg=-undefined', 'link-arg=dynamic_lookup']
    if 'CARGO_ENCODED_RUSTFLAGS' in env:
        flags = env['CARGO_ENCODED_RUSTFLAGS'].split('\x1f') if env['CARGO_ENCODED_RUSTFLAGS'] else []
    elif 'RUSTFLAGS' in env: flags = env['RUSTFLAGS'].split()
    else:
        flags = [arg for flag in link_args for arg in ('-C', flag)]
        return ['--config', 'build.rustflags=' + json.dumps(flags)], None
    for flag in link_args:
        if flag not in flags and '-C'+flag not in flags: flags += ['-C', flag]
    return [], dict(env, CARGO_ENCODED_RUSTFLAGS='\x1f'.join(flags))


def build(
    root, # The project directory, whose `tool.maturin` gives the defaults (expands `~`)
    *,
    library=True, # Build the library target?
    binaries=False, # Build the binary targets?
    profile=None, # Cargo profile; None: `tool.maturin.profile`, or Cargo's default
    target=None, # Target triple; None: the host
    features=None, # Features to enable, as a list or a string; None: `tool.maturin.features`
    all_features=False, # Enable every feature? `tool.maturin`'s `all-features` also turns this on
    no_default_features=False, # Leave out the default features? `tool.maturin`'s `no-default-features` also turns this on
    for_maturin=False, # Use the macOS linker flags that maturin's own wheel build uses?
):
    "Build with Cargo and return the compiler artifacts of the workspace's packages."
    root = Path(root).expanduser()
    cfg = settings(root)['maturin']
    cmd = ['build', '--manifest-path', str((root/'Cargo.toml').resolve()), '--message-format=json-diagnostic-short']
    if library: cmd.append('--lib')
    if binaries: cmd.append('--bins')
    if profile := profile or cfg.get('profile'): cmd += ['--profile', profile]
    if target: cmd += ['--target', target]
    if features := features or cfg.get('features'):
        cmd += ['--features', features if isinstance(features, str) else ','.join(features)]
    for flag, active in [('all-features', all_features), ('no-default-features', no_default_features)]:
        if active or cfg.get(flag): cmd.append('--' + flag)
    extra, env = _maturin_flags(target, os.environ) if for_maturin else ([], None)
    with timing_lock(): result = subprocess.run(['cargo', *cmd, *extra], cwd=root, stdout=subprocess.PIPE, text=True, env=env)
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    for m in messages:
        if m.get('reason') != 'compiler-message': continue
        name = tomllib.loads(Path(m['manifest_path']).read_text())['package']['name']
        for line in m['message']['rendered'].splitlines(): print(f'[{name}] {line}', file=sys.stderr)
    result.check_returncode()
    workspace = members(root)
    return [m for m in messages if m.get('reason') == 'compiler-artifact' and Path(m['manifest_path']) in workspace]


def replace(source, destination):
    "Replace an installed artifact without changing the file held by running processes."
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f'{destination.name}.{os.getpid()}.tmp')
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)
    return destination


def develop(root):
    "Install a Python extension, opted-in binaries and wheel data files from one Cargo build."
    root = Path(root).expanduser()
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


def stage_binaries(root, profile=None, target=None, features=None, all_features=False, no_default_features=False):
    "Stage Python-free Cargo binaries for a maturin wheel build."
    root = Path(root).expanduser()
    cfg = settings(root)['maturin']
    if 'data' not in cfg: raise ConfigError(r'staging native binaries requires tool.maturin.data')
    dest = root/cfg['data']/'scripts'
    artifacts = build(root, library=False, binaries=True, profile=profile, target=target, features=features,
        all_features=all_features, no_default_features=no_default_features, for_maturin=True)
    if dest.exists(): shutil.rmtree(dest)
    return [replace(m['executable'], dest/Path(m['executable']).name) for m in artifacts if m.get('executable')]
