import logging
from fnmatch import fnmatch
from typing import Any

import msgspec
from packaging.version import InvalidVersion, Version

from getrel.github import Release, RepositoryInfo

logger = logging.getLogger(__name__)


class GitHubProject(msgspec.Struct):
    owner: str
    repository: str
    current_version: str
    asset_patterns: list[str]
    version_prefix: str = ""


class UpdateInfo(msgspec.Struct):
    project: GitHubProject
    current_version: str
    latest_version: str
    latest_release: Release
    matching_assets: list[str]


def normalize_version(version: str, prefix: str = "") -> str:
    """Remove common prefixes from version strings."""
    if prefix and version.startswith(prefix):
        version = version[len(prefix) :]

    # Remove common prefixes like 'v', 'release-', etc.
    for common_prefix in ["v", "release-", "version-", "ver-"]:
        if version.lower().startswith(common_prefix):
            version = version[len(common_prefix) :]
            break

    return version


def compare_versions(current: str, latest: str, prefix: str = "") -> bool:
    """
    Compare two version strings, returns True if latest > current.
    Uses semantic versioning when possible, falls back to string comparison.
    """
    current_norm = normalize_version(current, prefix)
    latest_norm = normalize_version(latest, prefix)

    try:
        current_sem = Version(current_norm)
        latest_sem = Version(latest_norm)
        return latest_sem > current_sem
    except InvalidVersion:
        # Fall back to string comparison
        logger.debug(
            "Using string comparison for versions: %s vs %s", current_norm, latest_norm
        )
        return latest_norm > current_norm


def match_assets(asset_patterns: list[str], asset_names: list[str]) -> list[str]:
    """Match asset names against patterns using fnmatch."""
    matching = []

    for pattern in asset_patterns:
        for asset_name in asset_names:
            if fnmatch(asset_name, pattern):
                matching.append(asset_name)

    return list(set(matching))  # Remove duplicates


async def check_updates(projects: list[GitHubProject]) -> list[UpdateInfo]:
    """
    Check for updates across multiple GitHub projects.
    Returns list of projects that have newer releases available.
    """
    from getrel.github import get_latest_releases_for_repos

    # Extract repository info for GraphQL query
    repositories = [(project.owner, project.repository) for project in projects]

    # Get latest releases from GitHub
    repo_infos = await get_latest_releases_for_repos(repositories)

    updates = []

    for project, repo_info in zip(projects, repo_infos):
        if not repo_info.latest_release:
            logger.info(
                "No releases found for %s/%s", project.owner, project.repository
            )
            continue

        latest_version = repo_info.latest_release.tag_name

        # Check if this is a newer version
        if compare_versions(
            project.current_version, latest_version, project.version_prefix
        ):
            # Find matching assets
            asset_names = [asset.name for asset in repo_info.latest_release.assets]
            matching_assets = match_assets(project.asset_patterns, asset_names)

            if matching_assets:
                updates.append(
                    UpdateInfo(
                        project=project,
                        current_version=project.current_version,
                        latest_version=latest_version,
                        latest_release=repo_info.latest_release,
                        matching_assets=matching_assets,
                    )
                )
                logger.info(
                    "Update available for %s/%s: %s -> %s (%d matching assets)",
                    project.owner,
                    project.repository,
                    project.current_version,
                    latest_version,
                    len(matching_assets),
                )
            else:
                logger.warning(
                    "Update available for %s/%s: %s -> %s, but no assets match patterns %s",
                    project.owner,
                    project.repository,
                    project.current_version,
                    latest_version,
                    project.asset_patterns,
                )
        else:
            logger.debug(
                "No update for %s/%s: %s (latest: %s)",
                project.owner,
                project.repository,
                project.current_version,
                latest_version,
            )

    return updates


def filter_updates_by_project(
    updates: list[UpdateInfo], project_filter: str | None = None
) -> list[UpdateInfo]:
    """Filter updates by project name pattern."""
    if not project_filter:
        return updates

    return [
        update
        for update in updates
        if fnmatch(
            f"{update.project.owner}/{update.project.repository}", project_filter
        )
    ]
