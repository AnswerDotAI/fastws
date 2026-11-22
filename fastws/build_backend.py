r"Maturin build backend whose editable installs skip Rust when configured with `metadata-only`, and whose wheels can include Cargo binaries."
import argparse, base64, csv, hashlib, io
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED
import maturin
from .cargo import settings, stage_binaries

METADATA_ONLY = 'metadata-only' # PEP 660 config setting set by workspace `pyproject.toml` so editable installs leave Rust builds to `cargo develop`


def _stage(config_settings, default_profile='release'):
    root = Path.cwd()
    tool = settings(root)
    if not tool.get('fastws', {}).get('native-binaries'): return
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--profile')
    parser.add_argument('--release', '-r', action='store_true')
    parser.add_argument('--target')
    parser.add_argument('--features', '-F', action='append')
    parser.add_argument('--all-features', action='store_true')
    parser.add_argument('--no-default-features', action='store_true')
    args, _ = parser.parse_known_args(maturin.get_maturin_pep517_args(config_settings))
    profile = args.profile or ('release' if args.release else tool['maturin'].get('profile', default_profile))
    stage_binaries(root, profile, args.target, args.features, args.all_features, args.no_default_features)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _stage(config_settings)
    return maturin.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    r"Build a metadata-only editable wheel when `config_settings` sets `metadata-only`; delegate to maturin otherwise."
    if str((config_settings or {}).get(METADATA_ONLY, '')).lower() in ('', '0', 'false'):
        _stage(config_settings, default_profile='dev')
        return maturin.build_editable(wheel_directory, config_settings, metadata_directory)
    with TemporaryDirectory() as tmp:
        info = Path(metadata_directory) if metadata_directory else Path(tmp)/prepare_metadata_for_build_editable(tmp, config_settings)
        name = info.name.removesuffix('.dist-info')
        tag = next(line.removeprefix('Tag: ') for line in (info/'WHEEL').read_text().splitlines() if line.startswith('Tag: '))
        files = {p.relative_to(info.parent).as_posix(): p.read_bytes() for p in info.rglob('*') if p.is_file() and p != info/'RECORD'}
        root = Path.cwd()
        if py_src := settings(root)['maturin'].get('python-source'): files[f'{name}.pth'] = f'{(root/py_src).resolve()}\n'.encode()
        record = f'{info.name}/RECORD'
        rows = [(path, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(), len(data))
            for path, data in files.items()]
        buffer = io.StringIO()
        csv.writer(buffer).writerows([*rows, (record, '', '')])
        files[record] = buffer.getvalue().encode()
        filename = f'{name}-{tag}.whl'
        with ZipFile(Path(wheel_directory)/filename, 'w', ZIP_DEFLATED) as wheel:
            for path, data in files.items(): wheel.writestr(path, data)
        return filename


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    if data := settings(Path.cwd())['maturin'].get('data'): (Path.cwd()/data).mkdir(parents=True, exist_ok=True)
    return maturin.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
build_sdist = maturin.build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
