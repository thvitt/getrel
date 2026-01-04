"""
Migration code from previous getrel versions.
"""

import logging
import tomllib
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from pprint import pformat
from sys import exc_info

import msgspec
import xdg.BaseDirectory

from getrel.cli import first_config_path

logger = logging.getLogger(__name__)


from urllib.parse import urlparse

from .actions import (
    Action,
    BinAction,
    GithubProject,
    LinkAction,
    ProjectState,
    Release,
    ScriptAction,
    UnpackAction,
)


def _make_action(action: str, source: str, arg: str | None = None) -> Action:
    match action:
        case "unpack":
            return UnpackAction(source=source, destination=arg)
        case "link":
            return LinkAction(source=source, link=arg)
        case "bin":
            if arg is None:
                return BinAction(source=source, bin=arg)
            elif "/" in arg:
                return BinAction(source=source, link=arg)
            else:
                return BinAction(source=source, bin=arg)
        case _:
            raise ValueError(f"Unknown action {action} for source {source}")


def convert_actions(actions: dict[str, dict[str, str] | str]) -> Iterable[Action]:
    for source, spec in actions.items():
        if isinstance(spec, dict):
            for action, arg in spec.items():
                if action not in {"download", "register"}:
                    yield _make_action(action, source, arg)
        elif spec not in {"download", "register"}:
            yield _make_action(spec, source)


def convert_project(name, project) -> GithubProject:
    path = urlparse(project["url"]).path
    try:
        owner, repo = path[1:].split("/", maxsplit=2)
    except ValueError as e:
        raise ValueError(f"Failed to parse path {path} in project {name}") from e

    actions = list(convert_actions(project.get("assets", {})))
    actions.extend(convert_actions(project.get("install", {})))
    if "postinstall" in project:
        actions.append(ScriptAction("", script=project["postinstall"]))
    new_project = GithubProject(
        name=name,
        kind="github",
        user=owner,
        repo=repo,
        download=list(project.get("assets", [])),
        install=actions,
        uninstall=[],
    )
    return new_project


def convert_file(old_config: Path, output: Path):
    with old_config.open("rb") as f:
        old_projects = tomllib.load(f)
    logger.info(
        "Converting %d projects from %s to %s", len(old_projects), old_config, output
    )
    output.mkdir(exist_ok=True, parents=True)
    for name, project in old_projects.items():
        new_project = convert_project(name, project)

        outpath = output.joinpath(name).with_suffix(".yaml")
        logger.info(
            "Writing project %s (%d install actions) to %s",
            new_project.name,
            len(new_project.install),
            outpath,
        )
        outpath.write_bytes(msgspec.yaml.encode(new_project))


def convert_state(state_dir: Path) -> ProjectState:
    old_state = msgspec.json.decode(Path(state_dir, "state.json").read_text())
    name = state_dir.parent.name

    try:
        installed = Release(
            published=datetime.fromisoformat(old_state["installed"]["date"]),
            version=old_state["installed"]["version"],
        )
    except Exception as e:
        logger.warning(
            "Error retrieving installed version for %s: %s. Treating as not installed.",
            name,
            e,
        )
        logger.debug("%s", pformat(old_state, depth=2, sort_dicts=False), exc_info=True)
        installed = None

    old_releases = None
    try:
        old_releases = msgspec.json.decode(Path(state_dir, "releases.json").read_text())
        if isinstance(old_releases["data"], Sequence):
            cand_release = old_releases["data"][0]
        else:
            cand_release = old_releases["data"]
        cand_version = cand_release["name"] or cand_release["tag_name"]
        cand_date = datetime.fromisoformat(cand_release["published_at"])
        description = cand_release["body"]
        available = Release(
            published=cand_date, version=cand_version, description=description
        )
    except Exception as e:
        logger.warning(
            "Error retrieving candidate version for %s: %s. Will need update", name, e
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "%s", pformat(old_releases, depth=3, sort_dicts=False), exc_info=True
            )
        available = None

    config_file = first_config_path("getrel", "projects", name + ".yaml")
    if config_file is not None and config_file.exists():
        configured = datetime.fromtimestamp(config_file.stat().st_mtime)
    else:
        logger.warning("No config file for %s", name)
        configured = None

    return ProjectState(
        name,
        installed=installed,
        available=available,
        installed_files=[Path(s) for s in old_state.get("installed_files", [])],
        configured=configured,
    )
