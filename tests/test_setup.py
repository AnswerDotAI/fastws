import os, shlex, subprocess, sys, pytest
from pathlib import Path
from fastgit import Git
from fastws import __version__
import fastws.core as core


def test_setup_preserves_existing_destinations(tmp_path):
    marker = tmp_path/'keep.txt'
    marker.write_text('personal work')
    with pytest.raises(SystemExit, match='exists'): core.ws_setup('org/base', str(tmp_path))
    assert marker.read_text() == 'personal work'
    link = tmp_path/'link'
    link.symlink_to(tmp_path/'missing')
    with pytest.raises(SystemExit, match='exists'): core.ws_setup('org/base', str(link))
    assert link.is_symlink() and not (tmp_path/'missing').exists()


@pytest.mark.slow
def test_setup_builds_an_isolated_workspace(tmp_path, monkeypatch, capsys):
    "Exercise real Git, uv, and the installed ws-sync; package dependencies may need network access."
    pkg = Path(core.__file__).resolve().parent.parent
    wheels = tmp_path/'wheels'
    subprocess.run(['uv', 'build', '--no-sources', '--wheel', '--out-dir', str(wheels), str(pkg)], check=True)
    seed = tmp_path/'base'
    seed.mkdir()
    (seed/'repos.txt').write_text('')
    (seed/'pyproject.tmpl').write_text(
        f'[project]\nname = "setup-test"\nversion = "0.0.1"\nrequires-python = ">=3.13"\n'
        f'dependencies = ["fastws-cli=={__version__}"]\n[tool.uv.workspace]\nmembers = ["./*"]\n')
    g = Git(seed, raise_exc=True)
    g.init()
    g.add('.')
    g.commit(m='bootstrap fixture')
    config = tmp_path/'gitconfig'
    config.write_text(f'[url "{seed.as_uri()}"]\ninsteadOf = git@github.com:org/base.git\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('UV_FIND_LINKS', str(wheels))
    other = tmp_path/'other-env'
    other.mkdir()
    (other/'keep.txt').write_text('untouched')
    for key in ('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT', 'UV_PROJECT', 'UV_WORKING_DIR'): monkeypatch.setenv(key, str(other))
    root = tmp_path/'new workspace'
    core.ws_setup('org/base', str(root), python=f'{sys.version_info.major}.{sys.version_info.minor}')
    assert sorted(p.name for p in other.iterdir()) == ['keep.txt']
    assert os.environ['VIRTUAL_ENV'] == str(other)
    assert (root/'.venv'/'pyvenv.cfg').exists()
    assert (root/'pyproject.toml').exists() and (root/'uv.lock').exists()
    env = dict(os.environ, VIRTUAL_ENV=str(root/'.venv'), UV_PROJECT_ENVIRONMENT=str(root/'.venv'),
        UV_PROJECT=str(root), UV_WORKING_DIR=str(root))
    subprocess.run([str(root/'.venv/bin/ws-status')], cwd=root, env=env, check=True)
    assert f'source {shlex.quote(str(root/".venv/bin/activate"))}' in capsys.readouterr().out
