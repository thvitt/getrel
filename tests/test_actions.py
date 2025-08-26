from pathlib import Path

import pytest
from more_itertools import first

from getrel.actions import Action, UnpackAction
from getrel.utils import WorkingDirectory


class DummyAction(Action):
    def __call__(self, files):
        pass


def test_expand_source(resources):
    action = DummyAction(source="*.zip")
    with WorkingDirectory(resources):
        expanded = list(action.expand_source())
        assert len(expanded) == 1
        assert expanded[0].exists()


def test_expand_source_cand():
    action = DummyAction(source="*.zip")
    expanded = list(action.expand_source(["foo.tar.gz", "foo.zip"]))
    assert expanded == ["foo.zip"]


def test_expand_abs(tmp_path):
    action = DummyAction(source=str(tmp_path.absolute()))
    assert tmp_path == first(action.expand_source())


@pytest.mark.parametrize(
    "archive",
    [
        "archive.tar",
        "archive.tar.bz2",
        "archive.tar.gz",
        "archive.tar.xz",
        # "archive.tar.zst",
        "archive.zip",
    ],
)
def test_unpack(resources: Path, tmp_path: Path, archive: str):
    files = []
    with WorkingDirectory(tmp_path):
        action = UnpackAction(source=str(resources / archive))
        action(files)
    expected = tmp_path / "data" / "data.txt"
    assert expected.exists()
    assert expected in files
