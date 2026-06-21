import logging
import os
from collections.abc import Container, Iterable, Sequence
from datetime import datetime
from fnmatch import fnmatch
from itertools import count
from pathlib import Path
from pprint import pformat
from typing import Any, cast
from urllib.parse import urlparse

from httpx import Client
from msgspec import Struct, convert
from rich.progress import Progress

from getrel.actions import GithubProject, Project, ProjectState, Release, Settings
from getrel.config import load_project_configs, load_project_states, save_state
from getrel.utils import WorkingDirectory, double_braces, first, split_list

logger = logging.getLogger(__name__)

GRAPHQL_API = "https://api.github.com/graphql"


class ProjectExistsError(ValueError):
    def __init__(self, project: Project):
        self.project = project
        super().__init__(f"Project already exists: {project}")


class Asset(Struct, frozen=True):
    name: str
    contentType: str  # noqa: N815
    downloadUrl: str  # noqa: N815
    downloadCount: int  # noqa: N815
    size: int


def run_queries(projects: Sequence[GithubProject], query: str, chunk_size: int = 50):
    """
    Runs the given GraphQL query for every project in the list of projects.

    Args:
        projects: list of projects this query should run on
        query: the graphql query template. This will be run through [format_map][format_map], so `{  }` need to be doubled,
               references to attributes of the respective projects can be provided using `{project.attribute}`
        chunk_size: We will run at most this many GraphQL queries with one HTTP request

    Yields:
        tuples (project, result dict) for each project

    Examples:
        `run_queries(projects, 'repository(owner: "{project.user}", name: "{project.repo}") {{ description }}')`
        will yield tuples like `(project, {'description': 'multiple cursors in neovim'})`

    """
    logger.debug(
        "Preparing query from %s, using %d projects (%s)",
        query,
        len(projects),
        projects,
    )
    queries = [
        f"r{i}: " + query.format_map({"project": project})
        for i, project in enumerate(projects)
    ]
    with Client(
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}, http2=True
    ) as client:
        for chunk_start in range(0, len(projects), chunk_size):
            query = (
                "query {"
                + "\n".join(queries[chunk_start : chunk_start + chunk_size])
                + "}"
            )
            # logger.debug("GraphQL Query: %s", query)
            response = client.post(GRAPHQL_API, json={"query": query})
            response.raise_for_status()
            answer = response.json()
            if "errors" in answer:
                logger.error("Chunk %d: %s", chunk_start, pformat(answer))
            if "data" in answer:
                for project_id, data in answer["data"].items():
                    project = projects[int(project_id[1:])]
                    yield project, data


def _split_github_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.hostname.endswith("github.com"):
        raise ValueError(f"Not a Github URL: {url}")
    parts = parsed.path.split("/")
    if not parts[0]:
        parts = parts[1:]
    logger.debug("Split %s to %s", url, parts)
    return parts[0], parts[1]


class GithubProjectManager:
    configs: dict[str, GithubProject]
    states: dict[str, ProjectState]

    def __init__(self):
        ours, non_github_projects = split_list(
            load_project_configs(), lambda p: isinstance(p, GithubProject)
        )
        self.configs = {project.name: project for project in ours}
        self.states = load_project_states()

    def update_project_state(self, project: str, result: dict[str, Any]) -> bool:
        """
        Updates the project state of the given project.

        Args:
            project: project name
            result: query result from github, as delivered by run_queries

        Returns:
            True if the project has a new release
        """
        if project not in self.states:
            self.states[project] = state = ProjectState(project)
            logger.debug("No known state for %s, creating one", project)
        else:
            state = self.states[project]
        state.description = result.get("description", state.description)

        if result.get("latestRelease"):
            releases = [result["latestRelease"]]
        elif "releases" in result:
            releases = result["releases"]["nodes"]
        else:
            releases = []

        if releases:
            latest_release_data = max(
                releases, key=lambda r: datetime.fromisoformat(r["publishedAt"])
            )
            latest_release = Release(
                published=datetime.fromisoformat(latest_release_data["publishedAt"]),
                version=latest_release_data.get("tagName")
                or latest_release_data.get("name"),
                long_version=latest_release_data.get("name")
                or latest_release_data.get("tagName"),
                description=latest_release_data.get("description"),
            )
            if state.available and latest_release > state.available:
                logger.info(
                    "%s: New release %s (%s)",
                    project,
                    latest_release.version,
                    latest_release.published.isoformat(),
                )
                state.available = latest_release
                return True
            elif state.available is None:
                logger.info(
                    "%s: Release %s available (%s)",
                    project,
                    latest_release.version,
                    latest_release.published.isoformat(),
                )
                state.available = latest_release
                return True
        else:
            logger.warning("%s does not have a release.", project)
        return False

    def look_for_new_versions(
        self, save=True, progress: Progress | None = None, prerelease: bool = False
    ):
        """
        Checks GitHub for all projects that have new releases.

        Args:
            save: if True, save the state file after updating
            progress: progress bar
            prerelease: if True, also check for prereleases

        Returns:
            updated states of all projects with new releases
        """
        new: list[ProjectState] = []
        projects = list(self.configs.values())
        if progress:
            task = progress.add_task("Checking for updates ...", total=len(projects))

        if prerelease:
            query_part = (
                "releases(first: 20) { nodes { tagName name publishedAt description } }"
            )
        else:
            query_part = "latestRelease { tagName name publishedAt description }"

        for config, result in run_queries(
            projects,
            f"""\
            repository(owner: "{{project.user}}", name: "{{project.repo}}") {{{{
                description
                {double_braces(query_part)}
            }}}}""",
        ):
            if self.update_project_state(config.name, result):
                new.append(self.states[config.name])
            if progress:
                progress.advance(task)

        if save:
            save_state(self.states)
        return new

    def updateable_projects(self):
        return [p.name for p in self.states.values() if p.updateable]

    def list_artifacts(
        self,
        projects_or_names: list[str] | list[GithubProject] | None = None,
        prerelease: bool = False,
    ):
        """
        Lists the artifact data for all given projects.

        Args:
            projects: The projects for which to fetch data. If none given, all 'updateable' projects are used.
            prerelease: if True, also check for prereleases

        Yields:
            a tuple (config, list[Artifact]) for each of the respective projects
        """
        if projects_or_names is None:
            projects = self.updateable_projects()
            configs = [self.configs[name] for name in projects]
            logger.debug("Selected all updateble projects: %s", projects)
        elif projects_or_names and isinstance(projects_or_names[0], GithubProject):
            configs = cast("list[GithubProject]", projects_or_names)
            projects = [config.name for config in configs]  # pyright: ignore[reportAttributeAccessIssue]
        else:
            projects = cast("list[str]", projects_or_names)
            configs = [self.configs[name] for name in projects if name in self.configs]
            projects = [config.name for config in configs]
        logger.info(
            "Fetching release info for %d projects: %s",
            len(projects),
            " ".join(projects),
        )

        if prerelease:
            query_part = """\
                releases(first: 20) {
                    nodes {
                        tagName
                        name
                        publishedAt
                        description
                        releaseAssets(first: 100) {
                            nodes {
                                name
                                contentType
                                downloadUrl
                                downloadCount
                                size
                            }
                        }
                    }
                }"""
        else:
            query_part = """\
                latestRelease {
                    tagName
                    name
                    publishedAt
                    description
                    releaseAssets(first: 100) {
                        nodes {
                            name
                            contentType
                            downloadUrl
                            downloadCount
                            size
                        }
                    }
                }"""

        for config, data in run_queries(
            configs,
            f"""
                repository(owner: "{{project.user}}", name: "{{project.repo}}") {{{{
                    description
                    {double_braces(query_part)}
                }}}}""",
            chunk_size=10,
        ):
            self.update_project_state(config.name, data)
            if "latestRelease" in data:
                raw_assets = data["latestRelease"].get("releaseAssets", {}).get("nodes")
            elif "releases" in data:
                # search for newest release with assets
                releases = data["releases"]["nodes"]
                releases.sort(
                    key=lambda r: datetime.fromisoformat(r["publishedAt"]), reverse=True
                )
                raw_assets = []
                for release in releases:
                    raw_assets = release.get("releaseAssets", {}).get("nodes")
                    if raw_assets:
                        break
            else:
                raw_assets = []

            if raw_assets:
                logger.debug("Parsing asset records %s", raw_assets)
                yield config, [convert(raw_asset, Asset) for raw_asset in raw_assets]
            else:
                logger.warning(
                    "%s: Release %s has no downloadable assets",
                    config.name,
                    self.states[config.name].available.version,
                )
        save_state(self.states)

    def installed(self, project: str | Project) -> Release | None:
        name = project.name if isinstance(project, Project) else str(project)
        if name in self.configs:
            if name not in self.states:
                self.states[name] = ProjectState(name)
            return self.states[name].installed

    def uninstall(
        self,
        project: GithubProject | str,
        keep: Container[Path] = [],
        delete_assets: bool = False,
    ):
        try:
            if not isinstance(project, Project):
                project = self.configs[project]
            if self.installed(project):
                project.do_uninstall(
                    self.states[project.name], keep=keep, delete_assets=delete_assets
                )
                save_state(self.states)
        except KeyError:
            if isinstance(project, str) and project in self.states:
                state = self.states[project]
                with WorkingDirectory(state.project_dir) as pd:
                    deleted = set()
                    for file in reversed(state.installed_files):
                        if (
                            delete_assets or state._is_external(file)
                        ) and file not in keep:
                            if file.is_dir():
                                file.rmdir()
                            else:
                                file.unlink(missing_ok=True)
                            deleted.add(file)
                state.installed_files = [
                    file for file in state.installed_files if file not in deleted
                ]
                save_state(self.states)
                if state.installed_files:
                    logger.warning(
                        "For %s, no configuration was found. %d files outside of the project directory %s have been removed, "
                        "%d files remain in the project directory. Rerun with --delete-assets to remove them, as well.",
                        project,
                        pd,
                        len(deleted),
                        len(state.installed_files),
                    )
                else:
                    logger.warning(
                        "For %s, no configuration was found. All %d files have been removed, "
                        "the project directory %s and the state will be cleared as well.",
                        project,
                        len(deleted),
                        pd,
                    )
                    pd.directory.rmdir()
                    del self.states[project]
                    save_state(self.states)
            else:
                logger.error("Unknown project: %s", project)

    def delete_config(self, project: GithubProject | str):
        if isinstance(project, str):
            if project in self.configs:
                project = self.configs[project]
            else:
                logger.error("No project configuration for project %s", project)
                return
        assert isinstance(project, GithubProject)
        project.project_file.unlink(missing_ok=True)
        del self.configs[project.name]

    def install_or_update(self, project: GithubProject, assets: Container[Path]):
        if self.installed(project):
            self.uninstall(project, keep=assets)
        project.do_install(self.states[project.name])

    def download(
        self,
        project: GithubProject,
        assets: Iterable[Asset],
        client: Client,
        progress: Progress,
    ):
        state = self.states[project.name]
        with WorkingDirectory(state.project_dir):
            selected_assets = [
                asset
                for asset in assets
                if any(
                    fnmatch(asset.name, expanded_pat)
                    for pat in project.download
                    for expanded_pat in Settings.load().expand_arch(pat)
                )
            ]
            logger.info(
                "%s: %d of %d artefacts: %s",
                project.name,
                len(selected_assets),
                len(list(assets)),
                ", ".join(a.name for a in selected_assets),
            )
            for asset in selected_assets:
                asset_path = Path(asset.name)
                task = progress.add_task(asset.name, total=asset.size)
                with (
                    client.stream(
                        "GET", asset.downloadUrl, follow_redirects=True
                    ) as resp,
                    asset_path.open("wb") as file,
                ):
                    for chunk in resp.iter_bytes():
                        file.write(chunk)
                        progress.advance(task, len(chunk))
                if state.installed_files is None:
                    state.installed_files = []
                if asset_path not in state.installed_files:
                    state.installed_files.append(asset_path)
                yield asset_path
                progress.stop_task(task)
                progress.remove_task(task)
            save_state(self.states)

    def prepare_project(self, url: str, prerelease: bool = False):
        owner, repo = _split_github_url(url)

        # do we already have this configuration?
        for project in self.configs.values():
            if project.user == owner and project.repo == repo:
                raise ProjectExistsError(project)

        # determine name
        name = ""
        if repo not in self.configs:
            name = repo
        elif f"{owner}-{repo}" not in self.configs:
            name = f"{owner}-{repo}"
        else:
            for i in count(1):
                if (name := f"{repo}{i}") not in self.configs:
                    break
        project = GithubProject(
            kind="github", user=owner, repo=repo, name=name, prerelease=prerelease
        )
        project, assets = first(self.list_artifacts([project], prerelease=prerelease))
        state = self.states[project.name]
        return project, state, assets
