r"Build and install Cargo artifacts for mixed Rust/Python projects."
import json, os, shutil, subprocess, sys, sysconfig
from pathlib import Path

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib


def settings(root):
    "Read the project's tool configuration."
    return tomllib.loads((root/'pyproject.toml').read_text()).get('tool', {})


def build(root, *, library=True, binaries=False, profile=None, target=None, no_default_features=False):
    "Build with Cargo and return this package's compiler artifacts."
    cfg = settings(root)['maturin']
    manifest = (root/'Cargo.toml').resolve()
    binding = (root/cfg.get('manifest-path', 'Cargo.toml')).resolve()
    cmd = ['cargo', 'build', '--manifest-path', str(manifest), '--message-format=json-render-diagnostics']
    if library: cmd.append('--lib')
    if binaries: cmd.append('--bins')
    if profile := profile or cfg.get('profile'): cmd += ['--profile', profile]
    if target: cmd += ['--target', target]
    if no_default_features: cmd.append('--no-default-features')
    else:
        if features := cfg.get('features'): cmd += ['--features', ','.join(features)]
        for flag in ('all-features', 'no-default-features'):
            if cfg.get(flag): cmd.append('--' + flag)
    out = subprocess.run(cmd, cwd=root, stdout=subprocess.PIPE, text=True, check=True).stdout
    messages = [json.loads(line) for line in out.splitlines()]
    return [m for m in messages if m.get('reason') == 'compiler-artifact' and Path(m['manifest_path']) in (manifest, binding)]


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
    paths = [replace(lib, dest.with_name(dest.name + sysconfig.get_config_var('EXT_SUFFIX')))]
    if binaries:
        env = Path(os.environ.get('VIRTUAL_ENV', sys.prefix))
        scripts = env/('Scripts' if os.name == 'nt' else 'bin')
        paths += [replace(m['executable'], scripts/Path(m['executable']).name) for m in artifacts if m.get('executable')]
    return paths


def stage_binaries(root, profile=None, target=None):
    "Stage Python-free Cargo binaries in maturin's wheel-data directory."
    cfg = settings(root)['maturin']
    dest = root/cfg['data']/'scripts'
    artifacts = build(root, library=False, binaries=True, profile=profile, target=target, no_default_features=True)
    if dest.exists(): shutil.rmtree(dest)
    return [replace(m['executable'], dest/Path(m['executable']).name) for m in artifacts if m.get('executable')]
