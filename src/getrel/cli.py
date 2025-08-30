from httpx import HTTPStatusError
from getrel.github import get_project_states
import logging
from asyncio.tasks import as_completed
from pathlib import Path
from typing import Annotated

import xdg.BaseDirectory
from cyclopts import App, Parameter
from rich.console import Console
from rich.logging import RichHandler

from getrel.config import load_project_configs
from getrel.convert import convert_file

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


def _first_config_path(*resource: str | Path) -> Path | None:
    cand = xdg.BaseDirectory.load_first_config(*resource)
    if cand:
        return Path(cand)
    else:
        return None


@app.command
def convert_old_config(
    input: Path | None = _first_config_path("getrel", "projects.toml"),  # noqa: A002, B008
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
async def list_projects():
    async for project in load_project_configs():
        print(project)


@app.command
async def query_projects():
    projects = [p async for p in load_project_configs()]
    try:
        print(get_project_states(projects))
    except HTTPStatusError as e:
        logger.error("%s:\n%s", e, e.response.text)
