r"Build and install Cargo artifacts for mixed Rust/Python projects."
import json, os, shutil, subprocess, sys, sysconfig
from pathlib import Path

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib


def settings(root):
    "Read the project's tool configuration."
    return tomllib.loads((root/'pyproject.toml').read_text()).get('tool', {})


def cargo(root, *args):
    "Run Cargo in the project and return its standard output."
    return subprocess.run(['cargo', *args], cwd=root, stdout=subprocess.PIPE, text=True, check=True).stdout


def members(root):
    "The manifests of the workspace's member packages."
    return {Path(p['manifest_path']) for p in json.loads(cargo(root, 'metadata', '--no-deps', '--format-version', '1'))['packages']}


def build(root, *, library=True, binaries=False, profile=None, target=None, no_default_features=False):
    "Build with Cargo and return the compiler artifacts of the workspace's packages."
    cfg = settings(root)['maturin']
    cmd = ['build', '--manifest-path', str((root/'Cargo.toml').resolve()), '--message-format=json-render-diagnostics']
    if library: cmd.append('--lib')
    if binaries: cmd.append('--bins')
    if profile := profile or cfg.get('profile'): cmd += ['--profile', profile]
    if target: cmd += ['--target', target]
    if no_default_features: cmd.append('--no-default-features')
    else:
        if features := cfg.get('features'): cmd += ['--features', ','.join(features)]
        for flag in ('all-features', 'no-default-features'):
            if cfg.get(flag): cmd.append('--' + flag)
    messages = [json.loads(line) for line in cargo(root, *cmd).splitlines()]
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
    "Install a Python extension and opted-in binaries from one Cargo build."
    tool = settings(root)
    cfg = tool['maturin']
    if 'python-source' not in cfg: raise SystemExit(r'cargo develop requires tool.maturin.python-source')
    binaries = tool.get('fastws', {}).get('native-binaries', False)
    artifacts = build(root, binaries=binaries, profile='test')
    artifact, = [m for m in artifacts if 'cdylib' in m['target']['crate_types']]
    lib, = [f for f in artifact['filenames'] if Path(f).suffix in ('.dylib', '.so', '.dll')]
    module = cfg.get('module-name', artifact['target']['name'])
    dest = root/cfg['python-source']/Path(*module.split('.'))
    replace(lib, dest.with_name(dest.name + sysconfig.get_config_var('EXT_SUFFIX')))
    if binaries:
        env = Path(os.environ.get('VIRTUAL_ENV', sys.prefix))
        scripts = env/('Scripts' if os.name == 'nt' else 'bin')
        for m in artifacts:
            if m.get('executable'): replace(m['executable'], scripts/Path(m['executable']).name)


def stage_binaries(root, profile=None, target=None):
    "Stage Python-free Cargo binaries in maturin's wheel-data directory."
    cfg = settings(root)['maturin']
    dest = root/cfg['data']/'scripts'
    artifacts = build(root, library=False, binaries=True, profile=profile, target=target, no_default_features=True)
    if dest.exists(): shutil.rmtree(dest)
    return [replace(m['executable'], dest/Path(m['executable']).name) for m in artifacts if m.get('executable')]
