from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from glob import glob
from operator import itemgetter
from os import environ, fspath, chdir
import re
from fnmatch import fnmatch
import zipfile
import tarfile

from pathlib import Path
from typing import Optional, Any, Generator

from . import config
from .utils import naturalsize, first, fetch_if_newer
import logging

logger = logging.getLogger(__name__)


class Installable:
    """
    Handles installation.

    An installable has an action, an optional source and an optional argument. The source is the file we
    work on, this file is expected to be in the project folder. The action determines what to do and corresponds to a
    method. The argument is passed to the action. The following actions are available:

    - link     (creates symlink, required arg: link path)
    - bin      (creates symlink to executable, optional arg: command name or link path)
    - unpack   (unpacks the source archive, optional arg: target path)
    - delete   (deletes the files or directories, optional arg: glob pattern)

    Configuration is generally introduced with the 'install' keyword and can be either in an asset or in the project.
    For assets, it’s install = spec, for the project it's install = { source_pattern = spec, ... }, where spec is
    eiter an action string or a mapping {action = argument}. source_pattern is a glob pattern matching a file
    relative to the project directory which has been produced by the installation.

    For example, consider the following configuration:

    ```toml
    [broot]
    url = "https://github.com/Canop/broot"
    release = "latest"
    post-install = "broot --print-shell-function zsh > ~/.zsh/br.zsh"

    [broot.asset]
    match = "*.zip"
    install = "unpack"

    [broot.install]
    "*-linux-musl/broot" = "bin"
    "broot.1" = { "link" = "~/.local/man/" }
    ```

    This will download the asset matching '*.zip' from the GitHub project 'Canop/broot' and unpack that file.
    It will then make the file matching `*-linux-musl/broot` executable and link it to ~/.local/bin, and it will
    link 'broot.1' from '~/.local/man/'. Finally, it will run the post-install script using the shell, which will
    call the freshly installed broot to generate the script in the command line.
    """

    ACTIONS = ['unpack', 'bin', 'link', 'delete', 'record']

    project: "GitHubProject"  # FIXME
    source: Optional[Path]
    spec: Any

    def __init__(self, project: "GitHubProject", source: Optional[Path], spec: Any):
        self.project = project
        self.source = source
        self.spec = spec

    def link(self, arg):
        """
        Create a symbolic link at argument that points to the source.

        Argument will have user and environment variables expanded.
        """
        assert self.source is not None
        link = config.expand_path(arg, self.project.name, asset=self.source.name)
        link.symlink_to(self.source)
        self.project.register_installed_file(link)
        logger.info('Linked %s from %s', self.source, link)
        return [link]

    def bin(self, arg=None):
        """
        Make the source executable, and then create a symbolic link at arg that points to it.

        If arg is missing, project.name will be used instead. arg will be expanded, and if
        the result is not absolute, it will be relative to ~/.local/bin.
        """
        assert self.source is not None
        self.source.chmod(0o755)
        if arg is None:
            arg = self.project.name
        link = config.expand_path(arg, project_name=self.project.name, source=self.source)
        if not link.is_absolute():
            link = Path.home() / '.local/bin' / arg
        return self.link(link)

    def unpack(self, path=None):
        """
        Unpack the source. If an argument is given, it's the target directory.
        """
        assert self.source is not None
        if path is None:
            path = self.source.parent  ## FIXME
        member_names = []
        if tarfile.is_tarfile(self.source):
            with tarfile.open(self.source) as tar:
                safe_members = [m for m in tar.getmembers() if
                                not (Path(m.name).is_absolute() or '..' in Path(m.name).parts)]
                if len(safe_members) < len(tar.getmembers()):
                    logger.warning('%s: The tarfile contains unsafe members which will not be extracted: %s',
                                   self, ', '.join(m.name for m in tar.getmembers() if m not in safe_members))
                tar.extractall(path, safe_members)
                logger.info('%s: Extracted tar archive %s to %s', self, self.source, path)
                member_names = [m.name for m in safe_members]
        elif zipfile.is_zipfile(self.source):
            with zipfile.ZipFile(self.source) as z:
                z.extractall(path)  # handles unsafe members itself
                logger.info('%s: Extracted zip archive %s to %s', self, self.source, path)
                member_names = z.namelist()  ## FIXME
        else:
            logger.error('%s: %s could not be identified as an archive, not unpacked.', self, self.source)

        project_directory = self.project.directory.resolve()    # type:ignore
        return [(path / member).resolve().relative_to(project_directory) for member in member_names]

    def delete(self, arg=None):
        """
        Delete the argument or the source, if no argument is provided.

        The process is as follows:
            - if an argument is given, the argument has variables and users expanded. Otherwise the source is used.
            - if the argument is relative, it is considered as a glob pattern relative to the project directory.
              Otherwise, it is considered a global glob pattern. The glob pattern is expanded.
            - The result of globbing is filtered such that only safe files remain. A file is considered safe
              if it is relative to the project directory or if it is in the cache of files installed by the
              project.
            - The files from the filtered list are deleted and removed from the cache.
        """
        with self.project.use_directory() as project_directory:
            if arg:
                candidates = self._expand_arg(arg)
            elif self.source:
                candidates = [self.source]
            else:
                raise ValueError('delete (without source) requires an argument')

            safe_candidates = sorted([c for c in candidates if c.is_relative_to(project_directory)
                                      or c in self.project.installed_files],
                                     key=lambda p: len(p.parts),
                                     reverse=True)
            deleted_candidates = []
            for candidate in safe_candidates:
                try:
                    if candidate.is_dir():
                        candidate.rmdir()
                    else:
                        candidate.unlink()
                    self.project.unregister_installed_file(candidate)
                    deleted_candidates.append(candidate)
                except IOError as e:
                    logger.warning('Cannot delete %s: %s', candidate, e)
            logger.info('Deleted %d files: %s', len(deleted_candidates), ', '.join(map(str, deleted_candidates)))
        return []

    def _expand_arg(self, arg):
        with self.project.directory as project_directory:
            arg_path = config.expand_path(arg, self.project.name)
            if arg_path.is_absolute():
                candidates = [Path(p) for p in glob(fspath(arg_path))]
            else:
                candidates = list(project_directory.glob(fspath(arg_path)))
            return candidates

    def record(self, arg):
        """
        Registers the file given as argument as generated. This can be used to register side effects
        from scripts etc.
        """
        files = self._expand_arg(arg)
        self.project.register_installed_file(*files)
        return [files]

    def _actions_from_mapping(self, action_specs: Mapping) -> list[Path]:
        """
        Runs the actions from the given mapping. Assumes to be in the project directory.

        Args:
            action_specs: A mapping of the form {action: args, action: args}, i.e. already normalized

        Returns:
            a (possibly empty) list of paths that have been created by the action.
        """
        _no_config = object()
        new_sources = []
        for action in self.ACTIONS:
            arg = action_specs.get(action, _no_config)
            if arg is not _no_config:
                new_sources.extend(getattr(self, action)(arg))
        for unknown_action in set(action_specs.keys()) - set(self.ACTIONS):
            logger.warning('Skipping unknown install action %s=%s', unknown_action, action_specs[unknown_action])
        logger.debug('Running %s for %s created new sources: %s', action_specs, self.source, new_sources)
        return new_sources

    def _run_actions(self, spec: Any):
        """
        Normalizes the action spec and runs _actions_from_mapping

        This is intended to be called with the value of the 'install' key.
        """
        if isinstance(spec, Mapping) and any(k in self.ACTIONS for k in spec.keys()):
            return self._actions_from_mapping(spec)
        elif isinstance(spec, list):
            return self._actions_from_mapping({action: None for action in spec})
        else:  # its an action string
            return self._actions_from_mapping({spec: None})

    def install(self, including_assets=True, check_extra_sources: Optional[list] = None):
        with self.project.directory:
            new_sources = []
            if check_extra_sources:
                new_sources.extend(check_extra_sources)
            if self is self.project and including_assets:
                for asset in self.project.get_assets():
                    asset.install()
            if hasattr(self, 'source') and self.source is not None:
                if 'install' in self.spec:
                    logger.debug('Running install rule %s for %s', self.spec['install'], self.source)
                    new_sources.extend(self._run_actions(self.spec['install']))
                elif not new_sources:
                    logger.warning('No install configuration in spec %s and no extra sources found', self.spec)

            project_spec = self.project.config.get('install', {})
            while new_sources:
                source = new_sources.pop(0)
                for pattern, spec in project_spec.items():
                    if fnmatch(source, pattern):
                        logger.debug('Identified install rule %s=%s for %s', pattern, spec, source)
                        new_sources.extend(Installable(self.project, source, spec)._run_actions(spec))


class GitHubProject(Installable):
    _directory: Optional[Path] = None

    def __init__(self, name: str, project_config: dict):
        self.name = name
        self.config = project_config
        self.augment_config()
        self.release_cache = config.JSONSettings(config.project_state_directory(self.name) / 'releases.json')
        self.asset_cache = config.JSONSettings(config.project_state_directory(self.name) / 'assets.json')

    @staticmethod
    def parse_github_url(url: str) -> tuple[str, str]:
        """
        Looks for user and project in a github url.

        Returns:
            user, repo
        """
        if m := re.match(r'https?://(?:[^/]+\.)?github.com/([^/?\s]+)/([^/?\s]+)', url):
            return m.group(1), m.group(2)
        else:
            raise ValueError(f'{url} is not the URL of a GitHub project')

    @classmethod
    def from_url(cls, url: str, name: str | None = None) -> 'GitHubProject':
        """
        Creates a project configuration from the given GitHub URL.

        Returns:
            a GitHubProject, which may be a new one. Note this automatically adds configuration 

        Raises:
            ValueError if the URL could not be parsed
        """

        user, repo = cls.parse_github_url(url)
        if name is None:
            name = repo
        with config.edit_projects() as projects:
            project_config = projects.setdefault(name, {'github': f'{user}/{repo}'})
            return cls(name, project_config)

    def augment_config(self):
        if 'github' not in self.config and 'url' in self.config:
            if m := re.match(r'https?://(?:[^/]+\.)?github.com/(\w+)/(\w+)', self.config['url']):
                self.user = m.group(1)
                self.repo = m.group(2)
                self.config['github'] = self.user + '/' + self.repo
                if self.name is None:
                    self.name = self.repo
        elif 'github' in self.config:
            self.user, self.repo = self.config['github'].split('/')
            self.config['url'] = f'https://github.com/{self.user}/{self.repo}'

    @property
    def directory(self):
        """
        Returns the project directory (usually ~/.local/share/$APP_NAME/$PROJECT_NAME). Guaranteed to exist.
        """
        if not self._directory:
            self._directory = config.project_directory(self.name)
            self._directory.mkdir(parents=True, exist_ok=True)
        return self._directory

    @contextmanager
    def use_directory(self):
        """
        work in self.directory.

        Example:
            with project.use_directory() as pd:
                assert Path.cwd() == pd
        """

        old_cwd = Path.cwd()
        chdir(self.directory)
        yield self.directory
        chdir(old_cwd)

        

    def update(self) -> bool:
        """
        Update metadata. Returns True if we need a new 'download'.
        """
        update_url = f'https://api.github.com/repos/{self.user}/{self.repo}/releases'
        release_config = self.config.get('release')
        if release_config == 'latest':
            update_url += '/latest'
        with self.release_cache as cache:
            releases_updated = fetch_if_newer(update_url, cache,
                                              json='application/vnd.github+json')  # type:ignore - will be bool
            if releases_updated:
                selected_release = self.select_release()
                if selected_release:
                    cache['selected_release'] = selected_release['tag_name']
                    logger.info('%s: New release %s available', self.name, selected_release)
                    return True
                else:
                    cache['selected_release'] = None
                    logger.warn('%s: No release matching %s found.', self.name, release_config)
                    return False  # no release, no update
            else:
                logger.debug('%s: Releases not updated.', self.name)
                return False

    @property
    def releases(self):
        releases = self.release_cache.get('data')
        if isinstance(releases, Mapping):
            return [releases]
        else:
            return sorted(releases, key=itemgetter('created_at'), reverse=True)  # type:ignore - if its not a list, its a mapping

    def select_release(self):
        release_config = self.config.get('release', '')
        releases = self.releases
        if release_config == 'latest':
            return first((release for release in releases if not release['prerelease'] and not release['draft']),
                         default=None)
        elif release_config == 'pre':
            return first((release for release in releases if not release['draft']), default=None)
        else:
            return first((release for release in releases if
                          fnmatch(release['tag_name'], release_config) and not release['draft']), default=None)

    def get_assets(self, release=None, configured=True) -> list['GithubAsset']:
        result = []
        if release is None:
            release = self.select_release()
        if release is None:
            return result
        if configured:
            for spec in self.config['assets']:
                matching_descs = [asset for asset in release['assets'] if fnmatch(asset['name'], spec['match'])]
                if len(matching_descs) == 0:
                    logger.warn('%s %s: No asset matching %s found', self.name, release['tag_name'], spec['match'])
                else:
                    result.append(GithubAsset(self, release['tag_name'], spec, matching_descs[0]))
                    if len(matching_descs) > 1:
                        logger.warn(
                            '%s %s: %d assets match %s (%s). This is not supported, arbitrarily using the first one.',
                            self.name, release['tag_name'], len(matching_descs), spec,
                            ', '.join(a['name'] for a in matching_descs))
        else:
            for desc in release['assets']:
                result.append(GithubAsset(self, release['tag_name'], None, desc))
        return result

    def configure_asset(self, asset: "GithubAsset"):
        """Adds or updates the configuration for the given asset in this project."""
        assets = self.config.setdefault('assets', [])
        assert asset.spec is not None, 'Cannot add an unconfigured asset'
        pattern = asset.spec['match']
        existing_spec = first((a for a in assets if a['match'] == pattern), default=None)
        if existing_spec:
            existing_spec.update(asset.spec)
            asset.spec = existing_spec
        else:
            assets.append(asset.spec)

    def upgrade(self, update=True):
        if update:
            self.update()
        needs_install = False
        with self.asset_cache:
            for asset in self.get_assets():
                try:
                    asset_needs_install = bool(asset.download())
                    needs_install |= asset_needs_install
                    if asset_needs_install:
                        asset.install()
                except Exception as e:
                    logger.exception('Failed to install %s for %s: %s', asset.source.name, self.name, e)
        if needs_install:
            self.install()

    @property
    def installed_files(self) -> list[str]:
        """
        The list of files installed by this project's install() routine.

        TODO: proper Path and resolve() handling 
        """
        return self.release_cache.setdefault('installed_files', [])

    def register_installed_file(self, *files):
        file_list = self.installed_files
        for file in map(fspath, files):
            if file not in file_list:
                file_list.append(file)

    def unregister_installed_file(self, *files):
        for file in map(fspath, files):
            if file in self.installed_files:
                self.installed_files.remove(file)

    def exec_script(self, script: str, record_new_files: Optional[list] = None) -> int:
        """
        Executes the given script.

        If the script starts with #!, it is saved to a temporary file that is made executable
        and then launched. Otherwise, it is run with python subprocess's shell=True feature.
        The working directory will be the project directory. Additionally, the variables PROJECT (with the
        project name) and PROJECT_DIR (with the project directory) will be defined.

        if record_new_files is a list, new files _in the project directory_ will be recorded and added to
        the list. Files created by the script outside the project directory will never be detected.

        Returns:
            the script's exit code
        """
        from subprocess import run
        from tempfile import NamedTemporaryFile

        with self.directory as project_directory:
            if record_new_files is not None:
                files_before = set(project_directory.glob('**/*'))
            else:
                files_before = set()
            project_env = dict(environ)
            project_env['PROJECT'] = self.name
            project_env['PROJECT_DIR'] = fspath(project_directory)
            if script[:2] == '#!':
                with NamedTemporaryFile("wt", delete=False) as scriptfile:
                    scriptfile.write(script)
                    scriptpath = Path(scriptfile.name)
                try:
                    scriptpath.chmod(0o700)
                    result = run([scriptpath], env=project_env, cwd=project_directory)
                finally:
                    scriptpath.unlink()
            else:
                result = run(script, shell=True, env=project_env, cwd=project_directory)
            if record_new_files is not None:
                files_after = set(project_directory.glob('**/*'))
                new_files = files_after - files_before
                record_new_files.extend(new_files)
            return result.returncode


class GithubAsset(Installable):
    project: GitHubProject
    relese: str
    spec: MutableMapping | None
    asset_desc: Mapping
    cache: MutableMapping
    needs_download: bool
    source: Path

    def __init__(self, project: GitHubProject, release: str, spec: MutableMapping | None, asset_desc: Mapping) -> None:
        """
        Each asset is associated with:
            - the current project
            - a release (in form of a concrete release ID)
            - an asset specification (part of the project configuration)
            - an asset cache configuration 
        """
        self.project = project
        self.release = release
        self.spec = spec
        self.asset_desc = asset_desc
        self.source = config.project_directory(self.project.name) / self.asset_desc['name']

    @property
    def configured(self):
        return self.spec is not None

    def configure(self, spec: MutableMapping | None = None, **kwargs):
        """
        Adds or updates the asset's configuration in the project.
        """
        if self.spec is None:
            if spec is None:
                raise ValueError('Asset is not configured, need a spec')
            else:
                self.spec = spec
                self.project.configure_asset(self)
        else:
            if spec:
                self.spec.update(spec)
            self.spec.update(kwargs)

        logger.debug(self.spec)

        if self.spec['match'] not in self.project.asset_cache:
            self.project.asset_cache[self.spec['match']] = {}
        self.cache = self.project.asset_cache[self.spec['match']]  # type: ignore
        self.needs_download = self.release != self.cache.get('release')  # type: ignore
        logger.debug('Configured spec=%s for asset %s', self.spec, self)

    def __str__(self):
        asset = self.asset_desc
        title = asset['name']
        if asset['label'] and asset['label'] != title:
            title += f' "{asset["label"]}"'
        title += f' ({naturalsize(asset["size"])}, {asset["download_count"]} downloads)'
        return title

    def download(self, force: bool = False):
        with self.project.asset_cache:
            if force or self.needs_download:
                updated = fetch_if_newer(self.asset_desc['url'], self.cache,
                                         download_file=self.source,
                                         headers={'Accept': 'application/octet-stream'},
                                         stream=True)
                if updated:
                    self.cache.setdefault('files', []).append(self.source.name)
                return updated
            else:
                return False
