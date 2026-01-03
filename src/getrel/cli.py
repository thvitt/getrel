import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import msgspec
import xdg.BaseDirectory
from cyclopts import App, Parameter
from httpx import HTTPStatusError
from rich import get_console
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Column, Table
from rich.text import Text

from getrel.actions import BinAction, ProjectState
from getrel.config import (
    first_config_path,
    load_project_configs,
    load_project_states,
    save_state,
)
from getrel.convert import convert_file, convert_state
from getrel.github import GithubProjectManager
from getrel.utils import enc_hook

logger = logging.getLogger(__name__)

app = App()
app.register_install_completion_command(add_to_startup=False)


@app.meta.default
def prepare(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: Annotated[int, Parameter(alias="-v", count=True)] = 0,
):
    logging.basicConfig(
        level=logging.WARNING - 10 * verbose,
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True))],
    )
    app(tokens)


@app.command
def convert_old_config(
    input: Path | None = first_config_path("getrel", "projects.toml"),  # noqa: A002, B008
    /,
    output: Annotated[Path, Parameter(alias="-o")] = Path(
        xdg.BaseDirectory.xdg_config_home, "getrel", "projects"
    ),
):
    """
    Convert the old getrel configuration to the new format.
    """
    if input is None:
        logger.critical("No old getrel config found. Exiting.")
        return 2
    convert_file(input, output)


@app.command
def convert_old_state():
    states: dict[str, ProjectState] = {}
    for root in xdg.BaseDirectory.load_data_paths("getrel"):
        for project in Path(root).iterdir():
            if not project.is_dir():
                continue
            name = project.name
            try:
                state = convert_state(project / ".getrel")
                states[name] = state
            except Exception as e:
                logger.error("Failed to read state for %s: %s", name, e)
    save_state(states)


def _format_binary(binary: Path):
    cmd = binary.name
    found = shutil.which(binary.name)
    if not found:
        return Text(str(binary), style="warning")
    elif binary.samefile(found):
        return Text(cmd, style="bold")
    else:
        return Text(cmd, style="red")


@app.command(name="list")
def list_projects(projects: list[str] | None = None):
    configs = {project.name: project for project in load_project_configs()}
    states = load_project_states()
    if projects is None:
        projects = list({*configs, *states})
    table = Table(
        Column("Name", style="bold"),
        "Version ([green]update[/green], [red]not installed[/red])",
        "Binaries ([red]shadowed[/red], [dim]missing[/dim])",
        "description",
        box=None,
    )
    for project in projects:
        version = "?"
        description = "[dim]no info yet[/dim]"
        binaries = ""
        installed = False
        if project in states:
            state = states[project]
            description = state.description
            if state.installed:
                installed = True
                if (
                    state.available
                    and state.available.published > state.installed.published
                ):
                    version = f"{state.installed.version} → [bold green]{state.available.version}[/bold green]"
                else:
                    version = f"{state.installed.version}"
                binaries = Text(" ").join(
                    _format_binary(binary)
                    for binary in state.get_installed(
                        binary=True, external=True, absolute=True
                    )
                )
            elif state.available:
                version = f"[red]{state.available.version}"
                binaries = Text(
                    " ".join(
                        action.bin or action.source
                        for action in configs[project].install
                        if isinstance(action, BinAction)
                    ),
                    style="dim",
                )
        table.add_row(
            project,
            version,
            binaries,
            description,
            style="dim" if not installed else None,
        )
    get_console().print(table)


@app.command
def update():
    manager = GithubProjectManager()
    new = manager.look_for_new_versions()
    list_projects([p.name for p in new])
