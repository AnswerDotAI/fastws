r"Maturin build backend that includes Cargo binaries in Python wheels."
import argparse, base64, csv, hashlib, io
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED
import maturin
from .cargo import settings, stage_binaries


def _stage(config_settings):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--profile')
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--target')
    args, _ = parser.parse_known_args(maturin.get_maturin_pep517_args(config_settings))
    root = Path.cwd()
    profile = args.profile or ('release' if args.release else settings(root)['maturin'].get('profile', 'release'))
    stage_binaries(root, profile, args.target)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _stage(config_settings)
    return maturin.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    r"Build an editable wheel containing metadata and the Python source path, without compiling Rust."
    with TemporaryDirectory() as tmp:
        info = Path(metadata_directory) if metadata_directory else Path(tmp)/prepare_metadata_for_build_editable(tmp, config_settings)
        name = info.name.removesuffix('.dist-info')
        tag = next(line.removeprefix('Tag: ') for line in (info/'WHEEL').read_text().splitlines() if line.startswith('Tag: '))
        files = {p.relative_to(info.parent).as_posix(): p.read_bytes() for p in info.rglob('*') if p.is_file() and p != info/'RECORD'}
        root = Path.cwd()
        source = (root/settings(root)['maturin']['python-source']).resolve()
        files[f'{name}.pth'] = f'{source}\n'.encode()
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
    (Path.cwd()/settings(Path.cwd())['maturin']['data']).mkdir(parents=True, exist_ok=True)
    return maturin.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
build_sdist = maturin.build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
