import logging
import os
from collections.abc import Iterable, Sequence

from httpx import Client
import msgspec

from getrel.actions import GithubProject, ProjectState
from getrel.config import load_project_configs
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
        headers={"Authorization": f"Beaerar {os.environ['GITHUB_TOKEN']}"}, http2=True
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
            for project_id, data in response.json()["data"].items():
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
