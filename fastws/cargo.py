r"Build, test and install Cargo artifacts for mixed Rust/Python projects."
import json, os, re, shutil, signal, subprocess, sys, sysconfig, time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from . import store
from .procs import bounded
from ._toml import tomllib


class ConfigError(ValueError):
    "A project's `pyproject.toml` lacks a setting that a command needs."


def settings(root):
    "Read the project's tool configuration."
    return tomllib.loads((Path(root).expanduser()/'pyproject.toml').read_text()).get('tool', {})


def _dev_profile(root):
    return settings(root).get('fastws', {}).get('dev-profile', 'test') if (root/'pyproject.toml').exists() else 'test'


def cargo(root, *args):
    "Run Cargo in the project and return its standard output. A failure raises `CalledProcessError`, holding Cargo's standard error."
    return subprocess.run(['cargo', *args], cwd=Path(root).expanduser(), capture_output=True, text=True, check=True).stdout


def members(root):
    "The manifests of the workspace's member packages."
    return {Path(p['manifest_path']) for p in json.loads(cargo(root, 'metadata', '--no-deps', '--format-version', '1'))['packages']}


_TEST = re.compile(r'^test (.+) \.\.\. (ok|FAILED|ignored)')
_FAILURE = re.compile(r'^---- (.+) std(?:out|err) ----$')


class Diagnostic(NamedTuple):
    "A compiler message at its primary span, read from Cargo's JSON messages."
    path:str; line:int; col:int; level:str; message:str
    code:str|None=None # The error code or lint, such as `E0308` or `clippy::too_many_lines`
    def __str__(self): return f'{self.level}{f"[{self.code}]" if self.code else ""}: {self.path}:{self.line}: {self.message}'


def _run(root, args, env=None, diagnostics=True):
    "Run the Cargo subcommand and options `args` in `root`, with the environment variables `env` set: its exit status, its output, each `Diagnostic` once, and Cargo's other JSON messages, such as artifacts. With `diagnostics`, Cargo writes JSON messages and the output renders each compiler message short; without, the output is as Cargo printed it."
    flags = ('--message-format=json-diagnostic-short',) if diagnostics else ()
    p = bounded(['cargo', args[0], *flags, *args[1:]], seconds=None, merge=True, cwd=root, env=os.environ | (env or {}))
    if not diagnostics: return p.returncode, p.stdout, [], []
    text, found, rest = [], [], []
    for line in p.stdout.splitlines(keepends=True):
        if not line.startswith('{'): text.append(line); continue
        m = json.loads(line)
        if m['reason'] != 'compiler-message': rest.append(m); continue
        msg = m['message']
        text.append(msg['rendered'])
        if s := next((s for s in msg['spans'] if s['is_primary']), None):
            found.append(Diagnostic(s['file_name'], s['line_start'], s['column_start'], msg['level'], msg['message'], (msg['code'] or {}).get('code')))
    return p.returncode, ''.join(text), list(dict.fromkeys(found)), rest


def _lines(text, n=20, tail=False):
    "Up to `n` lines of `text` (None: all), indented, from its end if `tail`, with a count of the lines left out."
    lines = text.splitlines()
    if n is None or len(lines) <= n: return [f'    {l}' for l in lines]
    shown = [f'    {l}' for l in (lines[-n:] if tail else lines[:n])]
    more = f'    … {len(lines)-n} more lines'
    return [more, *shown] if tail else [*shown, more]


@dataclass
class Suite:
    "What one native test binary printed."
    name:str # The target source, such as `tests/core.rs` or `unittests src/lib.rs`
    output:str # Everything the binary printed, standard output and standard error merged
    tests:dict # Each test's outcome: `ok`, `FAILED` or `ignored`
    failures:dict # Each failing test's captured output
    summary:str # libtest's `test result:` line, empty for a binary that crashed or has its own harness
    failed:bool # Did the binary exit unsuccessfully?
    timed_out:bool=False # Was the binary killed for running out of time?

    @classmethod
    def read(cls, name, lines, returncode, timed_out=False):
        "Read one binary's lines and exit status, and whether it was killed for running out of time."
        failures, current = {}, None
        for line in lines:
            if m := _FAILURE.match(line): current = failures.setdefault(m[1], [])
            elif line == 'failures:': current = None
            elif current is not None: current.append(line)
        return cls(name, '\n'.join(lines), {m[1]: m[2] for l in lines if (m := _TEST.match(l))},
                   {k: '\n'.join(v).strip('\n') for k,v in failures.items()},
                   next((l for l in lines if l.startswith('test result: ')), ''), bool(returncode), timed_out)

    def __str__(self):
        n = Counter(self.tests.values())
        status = ', '.join(f'{n[o]} {w}' for o,w in [('ok', 'passed'), ('FAILED', 'failed'), ('ignored', 'ignored')] if n[o])
        if self.failed and not n['FAILED']:
            end = 'ran out of time' if self.timed_out else 'failed'
            status = f'{status}, then {end}' if status else end
        time = f' in {m[1]}' if (m := re.search(r'finished in (\S+)$', self.summary)) else ''
        return f'{self.name}: {status or "ok"}{time}'


@dataclass
class Results:
    "A Cargo run's outcome, its compiler messages and, for tests, each native test binary's results. It displays as a summary: the outcome, the compiler messages with errors first, each binary that ran tests, and each failure's output."
    command:str # The Cargo subcommand, such as `test` or `clippy`
    returncode:int
    text:str # The build's messages and each binary's merged standard output and standard error
    diagnostics:list # Each `Diagnostic` from the build, before the first binary ran
    suites:list=field(default_factory=list) # Each test binary's `Suite`, in execution order
    seconds:float=0 # Wall time spent executing native test binaries, excluding compilation

    @property
    def ok(self): return self.returncode == 0

    def summary(self, lines:int|None=20):
        "The outcome, compiler messages, each binary's result and each failure's output, with up to `lines` lines of each output (None: all)"
        res = [f'cargo {self.command} passed' if self.ok else f'cargo {self.command} failed with exit status {self.returncode}']
        diags = sorted(self.diagnostics, key=lambda d: d.level != 'error')
        res += [str(d) for d in diags[:lines]]
        if lines is not None and len(diags) > lines: res.append(f'{len(diags)-lines} more compiler messages in `diagnostics`')
        ran = [s for s in self.suites if s.tests or not s.summary]
        for s in ran:
            res.append(str(s))
            for t,out in s.failures.items(): res += [f'  FAILED {t}', *_lines(out, lines)]
            if s.failed and not s.failures: res += _lines(s.output, lines, tail=True)
        if idle := len(self.suites) - len(ran): res.append(f'{idle} test binaries ran no tests')
        if not (self.ok or self.suites or diags): res += _lines(self.text, lines, tail=True)
        return '\n'.join(res)
    __repr__ = summary


def check(
    root, # A Cargo package or workspace (expands `~`)
    *args, # The Cargo subcommand and its options, such as `'clippy', '--all-targets', '--', '-W', 'clippy::pedantic'`; none: `check --all-targets`
    env:dict=None, # Environment variables to set for the run, such as `{'CLIPPY_CONF_DIR': '/path/to/review'}`
    diagnostics:bool=True, # Ask Cargo for JSON compiler messages? False runs a subcommand that rejects `--message-format`, such as `llvm-lines`, with no diagnostics
)->Results:
    "Run a Cargo subcommand and return its `Results`, with each compiler message as a `Diagnostic`. A failing run is a result, not an exception."
    args = args or ('check', '--all-targets')
    return Results(args[0], *_run(Path(root).expanduser(), args, env, diagnostics)[:3])


def run_tests(
    root, # A Cargo package or workspace (expands `~`)
    *args, # Cargo build/selection options, such as `'--test', 'core'` or `'--profile', 'checked'`; `--no-fail-fast` runs every binary
    test_args=(), # Arguments passed directly to each test binary, such as `('name', '--nocapture')`
    env:dict=None, # Environment variables to set for the run, such as `{'BASEDPL_COSTS': '1'}`
    timeout:float=30 # Seconds that each native test binary may run after building; None: no limit
)->Results:
    "Build native tests once, with no time limit, then run Cargo's reported executables directly from their package directories. Source edits after building cannot trigger another build; results describe the compiled build, not the latest working tree. `timeout` bounds each binary on its own, excluding compilation. A binary that runs out of time is killed and reported as a failed binary whose suite is `timed_out`. An omitted profile uses tool.fastws.dev-profile, or test. Doctests are not run. This takes no timing lock: rustc's wrapper and tests that time own their holds."
    root, env = Path(root).expanduser().resolve(), {'CARGO_TERM_QUIET': 'false', 'CARGO_TERM_COLOR': 'never'} | (env or {})
    has_profile = any(a in ('-r', '--release', '--profile') or isinstance(a, str) and a.startswith('--profile=') for a in args)
    if not has_profile: args += ('--profile', _dev_profile(root))
    code, text, found, rest = _run(root, ('test', '--no-run', *args), env)
    if code: return Results('test', code, text, found)
    artifacts = [m for m in rest if m['reason'] == 'compiler-artifact' and m['profile']['test'] and m.get('executable')]
    suites, code, start = [], 0, time.monotonic()
    for m in artifacts:
        package = Path(m['manifest_path']).parent
        name = Path(m['target']['src_path']).relative_to(package).as_posix()
        if 'test' not in m['target']['kind']: name = 'unittests ' + name
        try: p, late = bounded([m['executable'], *test_args], timeout, merge=True, cwd=package, env=os.environ | env | {'CARGO_MANIFEST_DIR': str(package)}), False
        except TimeoutError as e: p, late = subprocess.CompletedProcess(m['executable'], -signal.SIGKILL, f'{e}\n'), True
        suites.append(Suite.read(name, p.stdout.splitlines(), p.returncode, late))
        text += f'     Running {name} ({m["executable"]})\n' + p.stdout
        if p.returncode:
            code = p.returncode
            if '--no-fail-fast' not in args: break
    return Results('test', code, text, found, suites, time.monotonic() - start)


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
    result = subprocess.run(['cargo', *cmd, *extra], cwd=root, stdout=subprocess.PIPE, text=True, env=env)
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


def develop(root, *, profile=None):
    "Serialize this project's cooperating develop runs through build, artifact capture, installation, generator and wheel data. An omitted profile uses tool.fastws.dev-profile, or test. Raw Cargo commands do not take this lock."
    root = Path(root).expanduser()
    with store.building(root) as started:
        tool = settings(root)
        cfg = tool['maturin']
        if 'python-source' not in cfg: raise ConfigError(r'cargo develop requires tool.maturin.python-source')
        binaries = tool.get('fastws', {}).get('native-binaries', False)
        artifacts = build(root, binaries=binaries, profile=profile or _dev_profile(root))
        artifact, = [m for m in artifacts if 'cdylib' in m['target']['crate_types']]
        lib, = [Path(f) for f in artifact['filenames'] if Path(f).suffix in ('.dylib', '.so', '.dll')]
        module = cfg.get('module-name', artifact['target']['name'])
        src = root/cfg['python-source']
        ext = src/Path(*module.split('.'))
        ext = ext.with_name(ext.name + sysconfig.get_config_var('EXT_SUFFIX'))
        env = Path(os.environ.get('VIRTUAL_ENV', sys.prefix))
        scripts = env/('Scripts' if os.name == 'nt' else 'bin')
        exes = [Path(m['executable']) for m in artifacts if binaries and m.get('executable')]
        package = src/module.split('.')[0]
        files = {f'python/{f.relative_to(src)}': f for f in package.rglob('*') if f.is_file() and f != ext and '__pycache__' not in f.parts}
        files |= {f'python/{ext.relative_to(src)}': lib} | {f'bin/{e.name}': e for e in exes}
        with store.access():
            kept = store.keep(root, files, dict.fromkeys(f for a in [lib, *exes] for f in store.dep_files(a)), started)
            store.install(root, kept, {ext: f'python/{ext.relative_to(src)}'} | {scripts/e.name: f'bin/{e.name}' for e in exes})
        store.collect()
        if generator := tool.get('fastws', {}).get('post-develop-module'):
            subprocess.run([sys.executable, '-m', generator], cwd=root, check=True)
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
