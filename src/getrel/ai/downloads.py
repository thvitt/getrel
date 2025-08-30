import asyncio
import logging
from pathlib import Path
from typing import Callable

import aiohttp

from getrel.releases import UpdateInfo

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


class DownloadResult:
    def __init__(
        self, asset_name: str, file_path: Path, success: bool, error: str | None = None
    ):
        self.asset_name = asset_name
        self.file_path = file_path
        self.success = success
        self.error = error


class DownloadProgress:
    def __init__(self, asset_name: str, total_size: int):
        self.asset_name = asset_name
        self.total_size = total_size
        self.downloaded = 0
        self.completed = False

    @property
    def progress_percent(self) -> float:
        if self.total_size == 0:
            return 100.0 if self.completed else 0.0
        return (self.downloaded / self.total_size) * 100


ProgressCallback = Callable[[DownloadProgress], None]


async def download_asset(
    session: aiohttp.ClientSession,
    asset_name: str,
    download_url: str,
    file_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> DownloadResult:
    """Download a single asset with progress tracking."""
    try:
        logger.info("Downloading %s to %s", asset_name, file_path)

        async with session.get(download_url) as response:
            if response.status != 200:
                error_msg = f"HTTP {response.status} downloading {asset_name}"
                logger.error(error_msg)
                return DownloadResult(asset_name, file_path, False, error_msg)

            total_size = int(response.headers.get("Content-Length", 0))
            progress = DownloadProgress(asset_name, total_size)

            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            with open(file_path, "wb") as f:
                async for chunk in response.content.iter_chunked(8192):
                    f.write(chunk)
                    progress.downloaded += len(chunk)

                    if progress_callback:
                        progress_callback(progress)

            progress.completed = True
            if progress_callback:
                progress_callback(progress)

            logger.info(
                "Successfully downloaded %s (%d bytes)", asset_name, progress.downloaded
            )
            return DownloadResult(asset_name, file_path, True)

    except Exception as e:
        error_msg = f"Error downloading {asset_name}: {e}"
        logger.error(error_msg)
        return DownloadResult(asset_name, file_path, False, error_msg)


async def download_update_assets(
    update: UpdateInfo,
    download_dir: Path,
    progress_callback: ProgressCallback | None = None,
    session: aiohttp.ClientSession | None = None,
) -> list[DownloadResult]:
    """Download all matching assets for a single update."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        results = []

        # Create project-specific directory
        project_dir = (
            download_dir
            / f"{update.project.owner}-{update.project.repository}-{update.latest_version}"
        )

        # Find assets to download
        assets_to_download = []
        for asset in update.latest_release.assets:
            if asset.name in update.matching_assets:
                file_path = project_dir / asset.name
                assets_to_download.append((asset.name, asset.download_url, file_path))

        # Download assets concurrently
        download_tasks = [
            download_asset(
                session, asset_name, download_url, file_path, progress_callback
            )
            for asset_name, download_url, file_path in assets_to_download
        ]

        results = await asyncio.gather(*download_tasks, return_exceptions=False)

        return results

    finally:
        if own_session:
            await session.close()


async def download_all_updates(
    updates: list[UpdateInfo],
    download_dir: Path,
    progress_callback: ProgressCallback | None = None,
    max_concurrent: int = 5,
) -> dict[str, list[DownloadResult]]:
    """
    Download assets for multiple updates with concurrency control.
    Returns a dict mapping project names to their download results.
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        # Create semaphore to limit concurrent downloads
        semaphore = asyncio.Semaphore(max_concurrent)

        async def download_with_semaphore(update: UpdateInfo):
            async with semaphore:
                return await download_update_assets(
                    update, download_dir, progress_callback, session
                )

        # Download all updates concurrently
        tasks = [download_with_semaphore(update) for update in updates]
        all_results = await asyncio.gather(*tasks, return_exceptions=False)

        # Map results to project names
        results_by_project = {}
        for update, results in zip(updates, all_results):
            project_key = f"{update.project.owner}/{update.project.repository}"
            results_by_project[project_key] = results

        return results_by_project


def create_simple_progress_callback() -> ProgressCallback:
    """Create a simple progress callback that logs download progress."""

    def callback(progress: DownloadProgress):
        if progress.completed:
            logger.info(
                "✓ %s completed (%d bytes)", progress.asset_name, progress.downloaded
            )
        elif progress.total_size > 0:
            logger.debug(
                "Downloading %s: %.1f%% (%d/%d bytes)",
                progress.asset_name,
                progress.progress_percent,
                progress.downloaded,
                progress.total_size,
            )

    return callback


async def download_single_update(
    update: UpdateInfo,
    download_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> list[DownloadResult]:
    """Convenience function to download assets for a single update."""
    return await download_update_assets(update, download_dir, progress_callback)
