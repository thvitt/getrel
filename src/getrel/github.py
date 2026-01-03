from datetime import datetime
import logging
import os
from collections.abc import Iterable, Sequence
from pprint import pformat
from turtle import st

import msgspec
from httpx import Client

from getrel.actions import GithubProject, ProjectState, Release
from getrel.cli import save_state
from getrel.config import load_project_configs, load_project_states
from getrel.utils import split_list

logger = logging.getLogger(__name__)

GRAPHQL_API = "https://api.github.com/graphql"


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
            logger.debug("GraphQL Query: %s", query)
            response = client.post(GRAPHQL_API, json={"query": query})
            response.raise_for_status()
            answer = response.json()
            if "errors" in answer:
                logger.error("Chunk %d: %s", chunk_start, pformat(answer))
            if "data" in answer:
                for project_id, data in answer["data"].items():
                    project = projects[int(project_id[1:])]
                    yield project, data


def get_project_states_old(projects: Sequence[GithubProject]):
    queries = [
        f"""
            r{i}: repository(owner: "{project.user}", name: "{project.repo}") {{
                owner {{ login }}
                name
                description
                latestRelease {{
                    tagName
                    name
                    publishedAt
                }}
            }}
        """
        for i, project in enumerate(projects)
    ]
    query = "query {" + "\n".join(queries) + "}"
    with Client(
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}, http2=True
    ) as client:
        logger.info("Requesting status from GitHub for %d projects", len(queries))
        logger.debug("GraphQL Query: %s", query)
        response = client.post(GRAPHQL_API, json={"query": query})
        response.raise_for_status()
        return response.json()


class GithubProjectManager:
    configs: dict[str, GithubProject]
    states: dict[str, ProjectState]

    def __init__(self):
        ours, non_github_projects = split_list(
            load_project_configs(), lambda p: isinstance(p, GithubProject)
        )
        self.configs = {project.name: project for project in ours}
        self.states = load_project_states()

    def look_for_new_versions(self, save=True):
        new: list[ProjectState] = []
        for config, result in run_queries(
            list(self.configs.values()),
            """\
            repository(owner: "{project.user}", name: "{project.repo}") {{
                description
                latestRelease {{
                    tagName
                    name
                    publishedAt
                    description
                }}
            }}""",
        ):
            if config.name not in self.states:
                self.states[config.name] = state = ProjectState(config.name)
                logger.debug("Found new state for %s", config.name)
            else:
                state = self.states[config.name]
            state.description = result.get("description", state.description)
            if "latestRelease" in result:
                latest_release = Release(
                    published=datetime.fromisoformat(
                        result["latestRelease"]["publishedAt"]
                    ),
                    version=result["latestRelease"].get("name")
                    or result["latestRelease"].get("tagName"),
                    description=result["latestRelease"].get("description"),
                )
                if state.available and latest_release > state.available:
                    logger.info(
                        "%s: New release %s (%s)",
                        config.name,
                        latest_release.version,
                        latest_release.published.isoformat(),
                    )
                    state.available = latest_release
                    new.append(state)
                elif state.available is None:
                    logger.info(
                        "%s: Release %s available (%s)",
                        config.name,
                        latest_release.version,
                        latest_release.published.isoformat(),
                    )
                    state.available = latest_release
                    new.append(state)
            else:
                logger.warning("%s does not have a release.", config.name)

        if save:
            save_state(self.states)
        return new
