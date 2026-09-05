import zipfile
from pathlib import Path

import pytest
from more_itertools import first
from rich.console import Console

import getrel.actions
from getrel.actions import (
    BaseAction,
    BinAction,
    Project,
    ProjectState,
    ScriptAction,
    UnpackAction,
)
from getrel.utils import WorkingDirectory


class DummyAction(BaseAction):
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
        "archive.tar.zst",
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


def test_do_install_ignores_leftover_files_from_previous_run(tmp_path, monkeypatch):
    """A source pattern should only match files known to the current install run,
    not leftover assets from a previously installed version sitting in the
    project directory (see BaseAction.expand_source, Project.do_install)."""
    monkeypatch.setattr(getrel.actions, "DATA_DIR", tmp_path)

    project_name = "myproj"
    project_dir = tmp_path / project_name
    project_dir.mkdir(parents=True)

    def make_zip(path: Path, member: str, content: str):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(member, content)

    make_zip(project_dir / "myproj-1.0.zip", "old.txt", "old")
    make_zip(project_dir / "myproj-2.0.zip", "new.txt", "new")

    project = Project(
        name=project_name,
        download=["myproj-*.zip"],
        install=[UnpackAction(source="myproj-*.zip")],
    )
    state = ProjectState(name=project_name, installed_files=[Path("myproj-1.0.zip")])

    project.do_install(state, run_files=[Path("myproj-2.0.zip")])

    assert (project_dir / "new.txt").exists()
    assert not (project_dir / "old.txt").exists()


def test_script_cmd(tmp_path: Path):
    files = []
    with WorkingDirectory(tmp_path):
        action = ScriptAction(cmd="pwd")
        action(files)
        assert files == [Path.cwd()]


def test_script_script(tmp_path: Path):
    files = []
    with WorkingDirectory(tmp_path):
        action = ScriptAction(script="touch foo && echo foo")
        action(files)
    assert files == [Path("foo")]


def test_script_shebang(tmp_path: Path):
    files = []
    with WorkingDirectory(tmp_path):
        action = ScriptAction(
            script="""#!/bin/env python3
with open("foo", "wt") as f:
    f.write("Hello world!")
print("foo")
"""
        )
        action(files)
    assert files == [Path("foo")]


def test_summarize_directory(tmp_path, monkeypatch):
    # Mock DATA_DIR to use our tmp_path
    monkeypatch.setattr(getrel.actions, "DATA_DIR", tmp_path)

    project_name = "test_project"
    project_dir = tmp_path / project_name
    project_dir.mkdir(parents=True)

    # Create some files
    zip_path = project_dir / "tool.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("test.txt", "content")

    bin_file = project_dir / "tool"
    bin_file.touch()
    bin_file.chmod(0o755)

    txt_file = project_dir / "README.md"
    txt_file.touch()

    subdir = project_dir / "docs"
    subdir.mkdir()
    (subdir / "info.txt").touch()

    # Mocking FileType to avoid libmagic dependency issues in tests if any,
    # but we can also use the real one if magic is installed.
    # The environment seems to have magic installed.

    project = Project(
        name=project_name, download=["*.zip"], install=[BinAction(source="tool")]
    )
    state = ProjectState(name=project_name)

    tree = state.summarize_directory(project)

    console = Console(width=100, force_terminal=True)
    with console.capture() as capture:
        console.print(tree)
    output = capture.get()

    # Assertions
    assert project_name in output
    assert "tool.zip" in output
    assert "tool" in output
    assert "README.md" in output
    assert "docs" in output
    assert "info.txt" in output

    # Check for matches
    assert "*.zip" in output
    assert "tool" in output
    assert "[match:" in output

    # Check for FileType indicators
    # tool.zip should be identified as an archive
    assert "archive" in output.lower()
    # tool should be identified as executable
    assert "executable" in output.lower()
