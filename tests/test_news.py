import pytest
from fastgit import Git

from fastws.news import _commit_context, _date, _release_context


@pytest.fixture
def history(tmp_path):
    g = Git(tmp_path, raise_exc=True)
    g.init('-b', 'main')
    g.config('user.email', 'test@example.com')
    g.config('user.name', 'Test')
    g.config('core.attributesFile', '/dev/null')
    g.remote('add', 'origin', str(tmp_path))
    for day, value in ((1, 'original'), (3, 'released'), (5, 'later')):
        (tmp_path/'feature.txt').write_text(value)
        (tmp_path/'cachy.jsonl').write_text(f'cache_{value}')
        g.add('.')
        date = f'2026-01-{day:02}T12:00:00Z'
        Git(tmp_path, pre=['env', f'GIT_COMMITTER_DATE={date}'], raise_exc=True).commit('-m', value, date=date)
        g.tag(f'v{day}')
    (tmp_path/'feature.txt').write_text('uncommitted')
    return tmp_path, Git(tmp_path, sync=False, raise_exc=True)


@pytest.mark.asyncio
async def test_date_snapshots_and_attributes(history):
    path, g = history
    start, end = _date('2026-01-02'), _date('2026-01-04')
    _, evidence = await _commit_context(g, 'main', start, end)
    assert '+released' in evidence and '+cache_released' in evidence
    assert 'later' not in evidence and 'uncommitted' not in evidence
    (path/'.git/info/attributes').write_text('cachy.jsonl -diff\n')
    _, evidence = await _commit_context(g, 'main', start, end)
    assert '+released' in evidence and 'cache_released' not in evidence and 'Binary files' in evidence
    _, evidence = await _commit_context(g, 'main', _date('2025-12-01'), end)
    assert '+released' in evidence
    assert not (await _commit_context(g, 'main', start, _date('2026-01-02T23:00:00Z')))[1]


@pytest.mark.asyncio
async def test_release_window_uses_tag_history_not_publication_order(history):
    _, g = history
    def release(tag, day, body='', prerelease=False):
        return dict(tag_name=tag, published_at=f'2026-01-{day:02}T12:00:00Z', body=body,
            html_url=f'https://example.com/{tag}', draft=False, prerelease=prerelease)
    releases = [release('v1', 6), release('v3', 4, '**Full Changelog**: https://example.com/compare/v1...v3'),
        release('v5', 5), release('preview', 4, 'Not stable', prerelease=True)]
    info, evidence = await _release_context(g, releases, _date('2026-01-04'), _date('2026-01-05'))
    assert 'v3' in info and 'v1' not in info and 'preview' not in info
    assert '-original' in evidence and '+released' in evidence
    assert 'later' not in evidence and 'uncommitted' not in evidence
    info, evidence = await _release_context(g, releases, _date('2026-01-05'), _date('2026-01-06T12:00:00Z'))
    assert 'v5' in info and 'v1' in info and 'v3' not in info
