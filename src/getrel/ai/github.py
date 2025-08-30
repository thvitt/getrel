import asyncio
import logging
import os
from typing import Any

import aiohttp
import msgspec

logger = logging.getLogger(__name__)


class GitHubError(Exception):
    pass


class RateLimitError(GitHubError):
    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(f"Rate limit exceeded, resets at {reset_at}")


class ReleaseAsset(msgspec.Struct):
    name: str
    download_url: str
    size: int


class Release(msgspec.Struct):
    tag_name: str
    name: str | None
    published_at: str
    assets: list[ReleaseAsset]


class RepositoryInfo(msgspec.Struct):
    owner: str
    name: str
    latest_release: Release | None


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise GitHubError(
                "GitHub token required. Set GITHUB_TOKEN environment variable."
            )

        self.base_url = "https://api.github.com/graphql"
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self.session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _execute_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.session:
            raise GitHubError("Client not initialized. Use async context manager.")

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        async with self.session.post(self.base_url, json=payload) as response:
            if response.status == 403:
                rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                if rate_limit_reset:
                    raise RateLimitError(int(rate_limit_reset))
                raise GitHubError("Forbidden - check token permissions")

            if response.status != 200:
                text = await response.text()
                raise GitHubError(f"HTTP {response.status}: {text}")

            data = await response.json()
            if "errors" in data:
                errors = data["errors"]
                raise GitHubError(f"GraphQL errors: {errors}")

            return data["data"]

    async def get_latest_releases(
        self, repositories: list[tuple[str, str]]
    ) -> list[RepositoryInfo]:
        if not repositories:
            return []

        # Split into batches of 100 (GraphQL complexity limit)
        batch_size = 100
        all_results = []

        for i in range(0, len(repositories), batch_size):
            batch = repositories[i : i + batch_size]
            batch_results = await self._get_releases_batch(batch)
            all_results.extend(batch_results)

        return all_results

    async def _get_releases_batch(
        self, repositories: list[tuple[str, str]]
    ) -> list[RepositoryInfo]:
        # Build GraphQL query for multiple repositories
        repo_queries = []
        variables = {}

        for i, (owner, name) in enumerate(repositories):
            alias = f"repo{i}"
            repo_queries.append(f"""
                {alias}: repository(owner: $owner{i}, name: $name{i}) {{
                    owner {{ login }}
                    name
                    latestRelease {{
                        tagName
                        name
                        publishedAt
                        releaseAssets(first: 100) {{
                            nodes {{
                                name
                                downloadUrl
                                size
                            }}
                        }}
                    }}
                }}
            """)
            variables[f"owner{i}"] = owner
            variables[f"name{i}"] = name

        query = f"""
            query GetLatestReleases({", ".join(f"$owner{i}: String!, $name{i}: String!" for i in range(len(repositories)))}) {{
                {" ".join(repo_queries)}
            }}
        """

        logger.debug("Executing GraphQL query for %d repositories", len(repositories))
        data = await self._execute_query(query, variables)

        results = []
        for i, (owner, name) in enumerate(repositories):
            alias = f"repo{i}"
            repo_data = data.get(alias)

            if not repo_data:
                logger.warning("No data returned for repository %s/%s", owner, name)
                results.append(
                    RepositoryInfo(owner=owner, name=name, latest_release=None)
                )
                continue

            latest_release_data = repo_data.get("latestRelease")
            if not latest_release_data:
                logger.info("No releases found for repository %s/%s", owner, name)
                results.append(
                    RepositoryInfo(owner=owner, name=name, latest_release=None)
                )
                continue

            # Parse assets
            assets = []
            asset_nodes = latest_release_data.get("releaseAssets", {}).get("nodes", [])
            for asset_data in asset_nodes:
                assets.append(
                    ReleaseAsset(
                        name=asset_data["name"],
                        download_url=asset_data["downloadUrl"],
                        size=asset_data["size"],
                    )
                )

            release = Release(
                tag_name=latest_release_data["tagName"],
                name=latest_release_data.get("name"),
                published_at=latest_release_data["publishedAt"],
                assets=assets,
            )

            results.append(
                RepositoryInfo(
                    owner=repo_data["owner"]["login"],
                    name=repo_data["name"],
                    latest_release=release,
                )
            )

        return results


async def get_latest_releases_for_repos(
    repositories: list[tuple[str, str]],
) -> list[RepositoryInfo]:
    async with GitHubClient() as client:
        return await client.get_latest_releases(repositories)
