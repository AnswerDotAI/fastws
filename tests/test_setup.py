import os, shutil, subprocess, pytest
from fastws._toml import tomllib
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


def test_sccache_config_merge_and_schema(tmp_path):
    ws1, ws2 = tmp_path/'ws1', tmp_path/'ws2'
    ws1.mkdir()
    ws2.mkdir()
    existing = 'server_startup_timeout_ms = 5000\n\n[dist]\nscheduler_url = "http://1.2.3.4:10600"\n'
    spec = core._parse_sccache_r2('https://r2id:r2sec@acct.r2.cloudflarestorage.com/aai-sccache')
    rendered = core._render_sccache_config(ws1, spec, existing)
    assert core._render_sccache_config(ws1, spec, rendered) == rendered
    merged = core._render_sccache_config(ws2, spec, rendered)
    data = tomllib.loads(merged)
    assert data['server_startup_timeout_ms'] == 5000 and data['dist']['scheduler_url'] == 'http://1.2.3.4:10600'
    assert data['basedirs'] == [str(ws1.resolve()), str(ws2.resolve())]
    pub = core._render_sccache_config(ws1, core._parse_sccache_r2('https://acct.r2.cloudflarestorage.com/pub/pfx'), '')
    if os.name != 'nt' and (sc := shutil.which('sccache')):
        env = dict(PATH=os.environ['PATH'], HOME=str(tmp_path), SCCACHE_SERVER_UDS=str(tmp_path/'sc.sock'))
        for name, text in (('auth', merged), ('pub', pub)):
            cfg = tmp_path/f'{name}.toml'
            cfg.write_text(text)
            r = subprocess.run([sc, '--stop-server'], env=env | {'SCCACHE_CONF': str(cfg)}, capture_output=True, text=True)
            assert 'Stopping sccache server' in r.stdout, r.stderr
