from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from datetime import datetime
from glob import glob
from operator import itemgetter
from os import environ, fspath, chdir
import re
from fnmatch import fnmatch
import zipfile
import tarfile

from pathlib import Path
from typing import Optional, Any

from .config import BaseSettings
from . import config
from .utils import naturalsize, first, fetch_if_newer
import logging

logger = logging.getLogger(__name__)


class Release(Mapping):
    version: str
    data: Mapping

    def __str__(self):
        return self.version

    def __repr__(self):
        return self.__str__()

    def __getitem__(self, key):
        return self.data[key]

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        return iter(self.data)


class ProjectFile:
    """
    Represents a file installed by a project.
    """

    project: "GitHubProject"
    path: Path
    asset: Optional["GithubAsset"] = None
    install: Optional[Mapping] = None
    external: bool = False

    def __init__(self, project: "GitHubProject", file: Path | str):
        self.project = project
        self.path = project.resolve_path(file)

        self.external = not self.path.is_relative_to(project.directory)

        for asset in project.get_assets():
            try:
                if self.path.samefile(asset.source):
                    self.asset = asset
                    break
            except OSError:
                if self.path == asset.source:
                    self.asset = asset
                    break

        if self.asset and 'install' in self.asset.spec:
            self.install = self.asset.spec['install']
        elif not self.external and 'install' in project.config:
            for pattern, action in project.config['install'].items():
                if fnmatch(project.project_relative_fspath(self.path), pattern):
                    self.install = action
                    break

    def __hash__(self):
        return hash(self.project.name) + hash(self.path)

    def __eq__(self, other):
        return isinstance(other, self.__class__) and other.project == self.project and other.path == self.path

    def __str__(self):
        return self.project.project_relative_fspath(self.path)


class GitHubRelease(Release):

    def __init__(self, release_record: Mapping):
        self.data = release_record
        self.version = release_record['tag_name']


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
        if link.is_symlink():
            logger.warning('Overwriting link %s (which pointed to %s) with %s',
                           link, link.readlink(), self.source)
            link.unlink()
        link.symlink_to(self.source.absolute())     # FIXME can we use 'intelligent' relative links here, cf. fetchlink?
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
            arg = self.source.stem #self.project.name
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
        extracted_files = [(path / member).resolve().relative_to(project_directory) for member in member_names]
        self.project.register_installed_file(*extracted_files)
        return extracted_files

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
        with self.project.use_directory():
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
                is_in_pd = source.absolute().is_relative_to(self.project.directory)
                for pattern, spec in project_spec.items():
                    matches_pattern = fnmatch(source, pattern)
                    if is_in_pd and matches_pattern:
                        logger.debug('Identified install rule %s=%s for %s', pattern, spec, source)
                        installable = Installable(self.project, source, spec)
                        new_sources.extend(installable._run_actions(spec))

    def __repr__(self):
        return f'<{self.__class__.__name} source={self.source} spec={self.spec!r}>'


class GitHubProject(Installable):
    """
    Properties:
        name (str): Project name, key in the config file
        config (Mapping): configured data about the project, project.toml[name], contains:
            - url (str): url to project on github
            - kind (str) = github
            - release (str): which release(s) to choose
            - assets (list[Mapping]): which files to download and what to do with them
            - install (Mapping): additional installation rules
            - post-install (str): additional installation script
        directory (Path): ~/.local/share/getrel/<name>, here everything is downloaded / installed

    States:
        configured (bool): has config with at least url, release, assets and one install or post-install rule somewhere
        updated: we do have cached release metadata and at least the following information:
            - date of last metadata update
            - latest release
            - selected release
            - probably information on the available assets
        downloaded [updated]: assets have been downloaded for the release
            - downloaded release
            - assets
        installed [downloaded]: install scripts have been run
            - files to uninstall


    Actions:
        [implied actions are run if needed]
        update: reads the release metadata from the server, may change state and available releases
        download: downloads the assets [update]
        install: runs the install and post-install rules on the downloaded assets [download]
        uninstall: removes files except for assets
        clear: uninstalls and then removes the project directory and state [uninstall]
        

    Additional Operations:

    """

    ## config -> use property config
    name: str
    _projects_config: Optional[BaseSettings] = None

    @property
    def config(self) -> MutableMapping:
        if self._projects_config is None:
            self._projects_config = config.edit_projects()
        if self.name not in self._projects_config:
            self.config = {}
        return self._projects_config[self.name]

    @config.setter
    def config(self, value: Mapping):
        if self._projects_config is None:
            self._projects_config = config.edit_projects()
        self._projects_config[self.name] = value

    def project_relative_fspath(self, orig: Path | str) -> str:
        """
        returns a string represantation that is relative to the project directory
        or absolute, with symlinks resolved
        """
        orig_path = self.resolve_path(orig)
        try:
            rel_path = orig_path.relative_to(self.directory)
        except ValueError:
            rel_path = orig_path
        return fspath(rel_path)

    @property
    def configured(self) -> bool:
        return all(k in self.config for k in ['url', 'release', 'assets'])

    def resolve_path(self, orig: Path | str) -> Path:
        """
        Returns an absolute path, resolved against the project directory
        """
        with self.use_directory():
            return Path(orig).absolute()

    @property
    def installed_files(self) -> list[str]:
        """
        The list of files installed by this project's install() routine. This always returns project relative paths.
        """
        if 'installed_files' not in self.state:
            self.state['installed_files'] = []
        return self.state['installed_files']

    def register_installed_file(self, *files):
        file_list = self.installed_files
        for file in map(self.project_relative_fspath, files):
            if file not in file_list:
                file_list.append(file)

    def unregister_installed_file(self, *files):
        for file in map(self.project_relative_fspath, files):
            if file in self.installed_files:
                self.installed_files.remove(file)
                logger.debug('unregistered %s', file)
            else:
                logger.debug('%s not registered, cannot unregister', file)

    def get_installed(self) -> list[ProjectFile]:
        return [ProjectFile(self, f) for f  in self.installed_files]

    def uninstall(self, keep_assets=False):
        with self.use_directory():
            parents = set()
            for project_file in sorted(self.get_installed(), key=lambda pf: len(pf.path.parts), reverse=True):
                try:
                    if not keep_assets or not project_file.asset:
                        if project_file.path.parent.is_relative_to(self.directory):
                            parents.add(project_file.path.parent)
                        if project_file.path.is_dir():
                            project_file.path.rmdir()
                        elif project_file.path.is_symlink() or project_file.path.exists():
                            project_file.path.unlink()
                            logger.debug('uninstalled %s', project_file)
                        else:
                            logger.warning('%s (belonging to %s) does not exist, so uninstalling it is a no-op', project_file, self)
                        self.unregister_installed_file(project_file.path)
                except IOError as e:
                    logger.error('Unable to delete %s (%s) while uninstalling %s', project_file, e, self)
        # now cleanup empty directories
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            try:
                if parent.exists() and parent != self.directory:
                    parent.rmdir()
            except IOError as e:
                logger.info('Keeping non-empty directory %s', parent)
        # persist changed state
        self.state['installed'] = None
        self.save()


    _directory: Optional[Path] = None

    def __init__(self, name: str, project_config: Optional[dict] = None):
        """
        Creates a new project.

        Args:
            name: Either the project's name or a GitHub URL or a user/repo string.
        """
        # The project needs a name before it can access its configuration. If project_config is given,
        # or if the name string is a name from the projects list, we assume there's no magic needed,
        # otherwise we try to parse the name string to identify the project URL.
        projects = config.edit_projects()
        if name not in projects and project_config is None:
            if re.match('https?://', name):
                user, repo = self.parse_github_url(name)
            elif m := re.match(r'([^/\s]+)/([^/\s]+)', name):
                user, repo = m.groups()
            else:
                raise ValueError(f'Project {name} needs an URL')
            url = f'https://github.com/{user}/{repo}'
            if repo in projects:
                ex_config = projects[repo]
                if 'url' in ex_config and ex_config['url'] != url:
                    raise ValueError(f'Project {name} already exists with URL {projects.get("url")} instead of {url}. Please provide an explicit name.')
            self.name = repo
            self.config['url'] = url
            self.config['kind'] = 'github'
            self.repo = repo
            self.user = user
        else:
            self.name = name
            if project_config is not None and self.config != project_config:
                logger.warning('Project %s: Overwriting previous config, %s, with new config %s', name, self.config, project_config)
                self.config = project_config
            self.user, self.repo = self.parse_github_url(self.config['url'])

        self.state = config.JSONSettings(config.project_state_directory(self.name) / 'state.json')
        self.release_cache = config.JSONSettings(config.project_state_directory(self.name) / 'releases.json')
        self.asset_cache = config.JSONSettings(config.project_state_directory(self.name) / 'assets.json')
        self.project = self

    def save(self):
        config.edit_projects().save()
        self.state.save()
        self.release_cache.save()
        self.asset_cache.save()

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
        #logger.debug('Changed into project directory: %s (from %s)', self.directory, old_cwd)
        yield self.directory
        chdir(old_cwd)
        #logger.debug('Changed back to %s', old_cwd)

    def update(self, all_releases=False) -> bool:
        """
        Update metadata. Returns True if we need a new 'download'.
        """
        update_url = f'https://api.github.com/repos/{self.user}/{self.repo}/releases'
        release_config = self.config.get('release')
        if release_config == 'latest' and not all_releases:
            update_url += '/latest'
        with self.release_cache as cache, self.state as state:
            releases_updated = fetch_if_newer(update_url, cache, message=f'Updating {self}',
                                              json='application/vnd.github+json')  # type:ignore # - will be bool
            state['updated'] = datetime.now().isoformat()
            if releases_updated:
                selected_release = self.select_release()
                if selected_release:
                    state['candidate'] = selected_release.version
                    logger.info('%s: New release %s available', self.name, selected_release)
                    return True
                else:
                    if release_config:
                        logger.warning('%s: No release matching %s found.', self.name, release_config)
                    state['candidate'] = None
                    return False  # no release, no update
            else:
                logger.debug('%s: Releases not updated.', self.name)
                return False

    @property
    def releases(self) -> list[Release]:
        releases = self.release_cache.get('data')
        if not releases:
            return []
        elif isinstance(releases, Mapping):
            return [GitHubRelease(releases)]
        else:
            return [GitHubRelease(r) for r in sorted(releases, key=itemgetter('created_at'), reverse=True)]  # type:ignore #- if its not a list, its a mapping

    def select_release(self) -> Optional[Release]:
        release_config = self.config.get('release', '')
        releases = self.releases
        if not releases:
            return None
        if release_config == 'latest':
            return first((release for release in releases if not release.data['prerelease'] and not release.data['draft']),
                         default=None)
        elif release_config == 'pre':
            return first((release for release in releases if not release.data['draft']), default=None)
        else:
            return first((release for release in releases if
                          fnmatch(release.version, release_config) and not release.data['draft']), default=None)

    def get_assets(self, release=None, configured=True) -> list['GithubAsset']:
        result = []
        if release is None:
            release = self.select_release()
        if release is None:
            return result
        if configured:
            for spec in self.config.get('assets', []):
                matching_descs = [asset for asset in release.data['assets'] if fnmatch(asset['name'], spec['match'])]
                if len(matching_descs) == 0:
                    logger.warning('%s %s: No asset matching %s found', self.name, release, spec['match'])
                else:
                    result.append(GithubAsset(self, release, spec, matching_descs[0]))
                    if len(matching_descs) > 1:
                        logger.warning(
                            '%s %s: %d assets match %s (%s). This is not supported, arbitrarily using the first one.',
                            self.name, release, len(matching_descs), spec,
                            ', '.join(a['name'] for a in matching_descs))
        else:
            for desc in release.data['assets']:
                result.append(GithubAsset(self, release, None, desc))
        return result

    def configure_asset(self, asset: "GithubAsset"):
        """Adds or updates the configuration for the given asset in this project."""
        if 'assets' not in self.config:
            self.config['assets'] = []
        assets = self.config['assets']
        assert asset.spec is not None, 'Cannot add an unconfigured asset'
        pattern = asset.spec['match']
        existing_spec = first((a for a in assets if a['match'] == pattern), default=None)
        if existing_spec:
            existing_spec.update(asset.spec)
            asset.spec = existing_spec
        else:
            assets.append(asset.spec)
            asset.spec = assets[-1]

    def unconfigure_asset(self, asset: "GithubAsset"):
        if asset.spec is None:
            logger.debug('Asset %s is unconfigured, no need to remove it', asset)
        else:
            assets = self.config['assets']
            pattern = asset.spec['match']
            existing_spec = first((a for a in assets if a['match'] == pattern), default=None)
            if existing_spec:
                assets.remove(existing_spec)

    def download(self):
        if self.needs_update:
            self.update()
        needs_install = False
        with self.asset_cache:
            for asset in self.get_assets():
                try:
                    asset_needs_install = bool(asset.download())
                    needs_install |= asset_needs_install
                except Exception as e:
                    logger.exception('Failed to download %s for %s: %s', asset.source.name, self.name, e)
        return needs_install

    def install(self, including_assets=True, force=False):
        if self.needs_update:
            self.update()
        needs_install = self.download()
        if needs_install or force:
            super().install(including_assets=including_assets)
        with self.state as state:
            state['installed'] = self.select_release().version

    @property
    def needs_install(self):
        return not self.state.get('installed')

    @property
    def needs_update(self):
        return not self.state.get('updated')

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

    def __str__(self):
        return self.name

class GithubAsset(Installable):
    project: GitHubProject
    release: str
    spec: MutableMapping | None
    asset_desc: Mapping
    cache: MutableMapping
    needs_download: bool
    source: Path

    def __init__(self, project: GitHubProject, release: Release, spec: MutableMapping | None, asset_desc: Mapping) -> None:
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
        if spec is None and 'assets' in project.config:
            self.spec = first((a for a in project.config['assets'] if fnmatch(asset_desc['name'], a['match'])), default=None)
        self.asset_desc = asset_desc
        self.source = config.project_directory(self.project.name) / self.asset_desc['name']

    @property
    def configured(self):
        return self.spec is not None

    def configure(self, spec: MutableMapping | None = None, **kwargs):
        """
        Adds or updates the asset's configuration in the project.
        """
        if spec is None and kwargs:
            spec.update(kwargs)
        if self.spec is None:
            if spec is None:
                raise ValueError('Asset is not configured, need a spec')
            else:
                self.spec = spec
                self.project.configure_asset(self)
        else:
            if spec:
                self.spec.update(spec)

        logger.debug(self.spec)

        if self.spec['match'] not in self.project.asset_cache:
            self.project.asset_cache[self.spec['match']] = {}
        self.cache = self.project.asset_cache[self.spec['match']]  # type: ignore
        self.needs_download = self.release != self.cache.get('release')  # type: ignore
        logger.debug('Configured spec=%s for asset %s', self.spec, self)

    def unconfigure(self):
        self.project.unconfigure_asset(self)
        self.spec = None

    def __str__(self):
        asset = self.asset_desc
        title = asset['name']
        if asset['label'] and asset['label'] != title:
            title += f' "{asset["label"]}"'
        title += f' ({naturalsize(asset["size"])}, {asset["download_count"]} downloads)'
        return title

    def download(self, force: bool = False):
        with self.project.asset_cache as cache:
            if self.asset_desc['url'] not in cache:
                cache[self.asset_desc['url']] = {}

            updated = fetch_if_newer(self.asset_desc['url'],
                                     cache[self.asset_desc['url']],
                                     download_file=self.source,
                                     message=str(self),
                                     headers={'Accept': 'application/octet-stream'},
                                     stream=True)
            if updated:
                self.project.register_installed_file(self.source)
            return updated


def get_project(name: str, must_exist: bool = True) -> GitHubProject:      # TODO refactor
    """
    Returns an existing project
    """
    projects = config.edit_projects()
    if name in projects:
        return GitHubProject(name)
    elif must_exist:
        raise KeyError(f'Project {name} does not exist.')
    else:
        project = err = None
        for cls in [GitHubProject]:
            try:
                project = cls(name)
                return project
            except Exception as e:
                err = e
        raise err