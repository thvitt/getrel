import logging
import tomllib
from collections.abc import Iterable
from pathlib import Path

import msgspec

logger = logging.getLogger(__name__)


from urllib.parse import urlparse

from .actions import (
    Action,
    BinAction,
    GithubProject,
    LinkAction,
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
