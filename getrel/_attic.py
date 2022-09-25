__version__ = '0.1.0'

import json
from dataclasses import dataclass
from time import sleep

from requests import Session
import questionary
import sys
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.console import Console
import re

session = Session()
console = Console()

# TODO refactor
def download_releases(project: str):
    if m := re.match(r'https://(?:[^/]+\.)?github.com/(\w+/\w+)', project):
        project = m.group(1)
    url = f'https://api.github.com/repos/{project}/releases'
    releases_resp = session.get(url, headers={'Accept': 'application/vnd.github.v3+json'})
    releases = releases_resp.json()
    stable_releases = sorted([rel for rel in releases if not rel['draft'] and not rel['prerelease']],
                             key=lambda r: r['created_at'], reverse=True)
    choices = [questionary.Choice(title=f'{r["name"]} ({r["tag_name"]})',
                                  value=r,
                                  disabled='no asset' if not r.get('assets', []) else None) for r in releases]
    latest = None
    use_latest = False
    if stable_releases:
        latest = stable_releases[0]
        choices.insert(0, questionary.Choice(title=f'Latest: {latest["name"]} (latest["tag_name"])',
                                                         value='latest',
                                                         disabled='no asset' if not latest.get('assets', []) else None))
    if not choices:
        console.log(releases)
    release = questionary.select('Which release do you want to download?', choices=choices, use_shortcuts=True, use_arrow_keys=True).ask()

    if not release:
        raise IOError(f'Project {project}: No release selected.')
    if release == "latest" and latest:
        use_latest = True
        release = latest
    if not 'assets' in release or not release['assets']:
        raise IOError(f'Project {project}, release {release["tag"]} does not have any downloadable assets')
    if len(release['assets']) > 1:
        choices = [questionary.Choice(title=f'{a["label"] or ""} ({a["name"]})', value=a) for a in release['assets']]
        asset = questionary.select('Select the file to download', choices=choices, use_shortcuts=True).ask()
    elif len(release['assets']) == 1:
        asset = release['assets'][0]

    with Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(asset['label'], filename=asset['name'], start=False)
        response = session.get(asset['url'], headers={'Accept': 'application/octet-stream'}, stream=True)
        progress.update(task_id, total=int(response.headers.get('Content-Length')))
        with open(asset['name'], 'wb') as file:
            progress.start_task(task_id)
            progress.console.print(response.headers)
            for chunk in response.iter_content(chunk_size=1024*1024):
                progress.update(task_id, advance=len(chunk))
                file.write(chunk)
        progress.stop_task(task_id)
        with open(asset['name'] + '.json', 'w') as f:
            json.dump(releases, f, indent=2)



def _main():
    download_releases(sys.argv[1])


if __name__ == '__main__':
    _main()
