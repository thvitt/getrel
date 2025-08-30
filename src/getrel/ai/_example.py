"""
Example usage of the GitHub Releases automation system.

This module demonstrates how to use the core functionality for checking
and downloading updates from GitHub releases.
"""

import asyncio
import logging
from pathlib import Path

from getrel.downloads import create_simple_progress_callback, download_all_updates
from getrel.releases import GitHubProject, check_updates


# Example projects configuration
EXAMPLE_PROJECTS = [
    GitHubProject(
        owner="BurntSushi",
        repository="ripgrep",
        current_version="14.0.0",
        asset_patterns=["*-x86_64-unknown-linux-gnu.tar.gz"],
        version_prefix="v",
    ),
    GitHubProject(
        owner="sharkdp",
        repository="fd",
        current_version="8.7.0",
        asset_patterns=["*-x86_64-unknown-linux-gnu.tar.gz"],
        version_prefix="v",
    ),
    GitHubProject(
        owner="dandavison",
        repository="delta",
        current_version="0.16.5",
        asset_patterns=["*-x86_64-unknown-linux-gnu.tar.gz"],
        version_prefix="",
    ),
]


async def main():
    """Example usage of the GitHub releases system."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("Checking for updates...")

    # Check for updates
    updates = await check_updates(EXAMPLE_PROJECTS)

    if not updates:
        print("No updates available.")
        return

    print(f"Found {len(updates)} updates:")
    for update in updates:
        print(
            f"  {update.project.owner}/{update.project.repository}: {update.current_version} -> {update.latest_version}"
        )
        print(f"    Assets: {', '.join(update.matching_assets)}")

    # Download updates
    download_dir = Path("./downloads")
    progress_callback = create_simple_progress_callback()

    print(f"\nDownloading to {download_dir.absolute()}...")
    results = await download_all_updates(updates, download_dir, progress_callback)

    # Report results
    print("\nDownload Results:")
    for project, project_results in results.items():
        print(f"  {project}:")
        for result in project_results:
            status = "✓" if result.success else "✗"
            print(f"    {status} {result.asset_name}")
            if not result.success:
                print(f"      Error: {result.error}")


if __name__ == "__main__":
    # Ensure you have GITHUB_TOKEN environment variable set
    asyncio.run(main())
