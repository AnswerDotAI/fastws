r"Maturin build backend that includes Cargo binaries in Python wheels."
import argparse
from pathlib import Path
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
    _stage(config_settings)
    return maturin.build_editable(wheel_directory, config_settings, metadata_directory)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    (Path.cwd()/settings(Path.cwd())['maturin']['data']).mkdir(parents=True, exist_ok=True)
    return maturin.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
build_sdist = maturin.build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel
get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
