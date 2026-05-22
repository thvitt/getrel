import os
from pathlib import Path

from getrel.actions import Settings
from getrel.utils import WorkingDirectory


def test_expand():
    os.environ["FOO"] = "bar"
    expanded = list(Settings().expand("~/$FOO/bar"))
    assert expanded == [Path.home() / "bar/bar"]


def test_working_directory(resources):
    old_cwd = Path.cwd()
    with WorkingDirectory(resources) as wd:
        assert Path.cwd() == resources
        assert wd.directory == resources
        assert wd.previous == old_cwd
    assert old_cwd == Path.cwd()
