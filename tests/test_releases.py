from fastcore.basics import AttrDict

import fastws.releases as relmod


def _rel(tag): return AttrDict(tag_name=tag)


def test_newest_tag_by_version_not_publish_order():
    assert relmod._newest_tag([_rel("v0.1.17"), _rel("v0.1.18"), _rel("v0.1.9")]) == "v0.1.18"
    assert relmod._newest_tag([]) is None




