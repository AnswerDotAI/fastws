import pytest
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


