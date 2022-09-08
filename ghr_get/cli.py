import os
import re
import sys
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Collection, Optional, List

import rich.table
from prompt_toolkit.document import Document
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.validation import ValidationError, Validator
import typer
from pygments.lexers.configs import TOMLLexer
from rich.table import Table

from .project import GitHubProject, GithubAsset, get_project
import questionary
from difflib import SequenceMatcher
import fnmatch
from itertools import chain
from .utils import FileType, first, unique_substrings
from .config import edit_projects

import logging
from rich.logging import RichHandler
from rich.console import Console
from rich.syntax import Syntax
import tomlkit

console = Console()

FORMAT = "%(message)s"
log_handler = RichHandler(console=console, show_time=False, rich_tracebacks=False)
logging.basicConfig(
    level="NOTSET", format=FORMAT, datefmt="[%X]", handlers=[log_handler]
)
logger = logging.getLogger(__name__)
app = typer.Typer(pretty_exceptions_enable=False)

@app.callback()
def initialize(verbose: int = typer.Option(0, '--verbose', '-v', count=True, help="more messages", show_default=False, show_choices=False),
               quiet: int = typer.Option(0, '--quiet', '-q', count=True, help="less messages", show_default=False, show_choices=False)):
    log_level = min(logging.CRITICAL, max(0, logging.WARNING + (10 * (quiet - verbose))))
    log_handler.setLevel(log_level)


class FNMatchValidator(Validator):

    def __init__(self, candidates: Collection[str] = tuple(), *,
                       must_match: Optional[str]=None, 
                       max_matches: Optional[int] = None,
                       min_matches: Optional[int] = None):
        super().__init__()
        self.candidates = list(candidates or [])
        self.must_match = must_match
        if must_match is not None and must_match in self.candidates:
            self.candidates.append(must_match)
        self.max_matches = max_matches
        self.min_matches = min_matches


    def validate(self, document: Document) -> None:
        matches = {c for c in self.candidates if fnmatch.fnmatch(c, document.text)}
        if self.must_match is not None and self.must_match not in matches:
            raise ValidationError(message=f'"{document.text}" does not match {self.must_match}: {matches}')
        elif self.max_matches is not None and len(matches) > self.max_matches:
            raise ValidationError(message=f'matches {len(matches)} items ({", ".join(matches)})')
        elif self.min_matches is not None and len(matches) < self.min_matches:
            raise ValidationError(message=f'matches only {len(matches)} instead of {self.min_matches} items')


class TOMLValidator(Validator):

    def validate(self, document: Document):
        try:
            tomlkit.loads(document.text)
        except tomlkit.exceptions.ParseError as e:
            raise ValidationError(message=str(e), cursor_position=document.translate_row_col_to_index(e.line, e.col))


def rel2choice(ghrelease: Mapping, special=None) -> questionary.Choice:
    title = ghrelease["tag_name"]
    name = ghrelease.get('name')
    if name and name != title:
        title += f' ({name})'
    if special:
        title = f'{special} – {title}'
    if ghrelease.get('prerelease', False):
        title += ' (prerelease)'
    return questionary.Choice(title, value=special or ghrelease["tag_name"])

def asset2choice(asset: GithubAsset) -> questionary.Choice:
    return questionary.Choice(str(asset), value=asset, checked=asset.configured)


@app.command()
def add(url: str, detailed: bool = typer.Option(False, "-d", "--detailed",
                                                help="ask even if we have a heuristic")):
    """
    Interactively install a tool and add its configuration.
    """
    with edit_projects() as settings:
        project = get_project(url, must_exist=False)
        if project.configured:
            console.print(f'The project [bold]{project.name}[/bold] is already configured:')
            list_([project.name], config=True)
            console.print('If you reconfigure it, [strike]it will be uninstalled first[/strike], and the '
                          'existing configuration will be partially deleted.')
            if questionary.confirm('Reconfigure the project?', default=False).ask():
                ... # TODO project.uninstall()
            else:
                return

        project.update(all_releases=True)
        special = dict(
            latest = first((release for release in project.releases if not release.data['prerelease'] and not release.data['draft']), default=None),
            pre = first((release for release in project.releases if not release.data['draft']), default=None))
        choices = []
        for label, release in special.items():
            if release:
                choices.append(rel2choice(release.data, label))
        choices.extend(rel2choice(r) for r in project.releases)

        if 'release' in project.config:
            configured_release = project.config['release']
            default_release_choice = first(c for c in choices if fnmatch.fnmatch(c.value, configured_release))
        else:
            default_release_choice = None

        selected = questionary.select('Which release would you like to use', choices,
                                      default=default_release_choice,
                                      use_arrow_keys=True, use_shortcuts=True).ask()
        logger.debug('Selected release %s', selected)
        if selected in {'latest', 'pre'}:
            project.config['release'] = selected
            release_record = special[selected]
        else:
            release_record = [p for p in project.releases if p['tag_name'] == selected][0]
            try:
                pattern = identifying_pattern([r.data['tag_name'] for r in project.releases], selected)
                if detailed and '*' in pattern:
                    pattern = questionary.text(f'Release matching pattern',
                            default=pattern,
                            validate=FNMatchValidator([c.value for c in choices], must_match=selected)).ask()
            except NoPatternError:
                pattern = selected
            project.config['release'] = pattern

        logger.debug('Project config: %s', project.config)
        logger.debug('Global settings:\n%s', settings)

        # now select the asset(s)
        asset_choices = [asset2choice(a) for a in project.get_assets(release=release_record, configured=False)]

        selected_assets = questionary.checkbox('Which asset(s) should be downloaded?', asset_choices).ask()
        for asset in project.get_assets():
            asset.unconfigure()

        for asset in selected_assets:
            asset.unconfigure()
            asset_name = asset.source.name
            asset_names = [a['name'] for a in release_record['assets']]
            try:
                pattern = identifying_pattern(asset_names, asset_name, version=release_record.version)
            except NoPatternError:
                pattern = asset_name

            if detailed:
                pattern = questionary.text(f'Asset matching pattern',
                        default=pattern,
                        validate=FNMatchValidator(asset_names, must_match=asset_name, max_matches=1),
                        instruction=f'edit/accept pattern for {asset_name}').ask()
            else:
                logger.info('Asset pattern for %s: %s', asset_name, pattern)
            asset.configure({'match': pattern})
            asset.download()
            # now let’s see what we got 
            kind = FileType(asset.source)
            if kind.executable:
                if detailed:
                    if questionary.confirm(f'{asset.source.name} seems to be an executable ({kind.description}). '
                            'Should I install it as binary {project.name}?').ask():
                        asset.configure({'install': 'bin'})
                    else:
                        bin = questionary.text('Use a different binary name?', instruction='Enter name relative to {Path.home() / ".local/bin"} or leave empty if it should not be installed').ask()
                        if bin:
                            asset.configure({'install': {'bin': bin}})
                else:
                    asset.configure({'install': 'bin'})
                    logger.info('Configured %s (%s) to be installed as binary %s', asset.spec['match'], asset.source, project.name)
            if not asset.spec.get('install') and kind.archive:
                if not detailed or questionary.confirm(f'{asset.source.name} is an archive ({kind.description}). '
                        'Should I unpack it?').ask():
                    asset.configure({'install': 'unpack'})
            if not asset.spec.get('install'):
                link = questionary.path('Symlink somewhere? You can use ~ and ${VAR}s', only_directories=True).ask()
                if link:
                    asset.configure({'install': {'link': link}})
            logger.debug('Running install for spec %s', asset.spec)
            asset.install()

        console.print(file_table(project, title="Installed Files", show_header=True, include_type=True))

        # now we've installed and configured the assets – let's look for other unconfigured files.
        project_files = {f: FileType(f.path) for f in project.get_installed() if not f.external}
        unconfigured = {f: t for (f, t) in project_files.items() if not f.install}
        executables = [f for (f, t) in unconfigured.items() if t.executable]
        if len(executables) == 1:
            try:
                pattern = identifying_pattern(map(str, project_files), str(executables[0]))
                if 'install' not in project.config:
                    project.config['install'] = {}
                project.config['install'][pattern] = 'bin'
                del unconfigured[executables[0]]
                console.print(f'Configured [bold]{pattern}[/bold] to be installed as command [bold]{project.name}[/bold]')
            except NoPatternError:
                logging.warning('Could not generate pattern for %s, leaving for manual config', executables[0])

        config_str = tomlkit.dumps(project.config)
        new_config_str = questionary.text('Edit project config',
                                          default=config_str,
                                          multiline=True,
                                          validate=TOMLValidator(),
                                          lexer=PygmentsLexer(TOMLLexer)).ask()
        if new_config_str != config_str:
            new_config = tomlkit.loads(new_config_str)
            project.config.clear()
            project.config.update(dict(new_config))

    project.save()


def file_table(project: GitHubProject, include_type=False, **kwargs):
    if 'box' not in kwargs:
        kwargs['box'] = None
    if 'show_header' not in kwargs:
        kwargs['show_header'] = False
    table = Table(**kwargs)
    table.add_column('File')
    table.add_column('Action', style='green')
    table.add_column('Asset?', style='cyan')
    table.add_column('External?', style='red')
    if include_type:
        table.add_column('File Type')
    for file in project.get_installed():
        cells = [str(file),
                      str(file.install) if file.install else '',
                      'A' if file.asset else '',
                      'X' if file.external else '']
        if include_type:
            cells.append(str(FileType(file.path)))
        table.add_row(*cells, style='bold' if file.external else 'dim' if file.install else '')
    return table



@app.command('list')
def list_(projects: List[str] = typer.Argument(None, help="Projects to list (omit for all)"),
          details: bool = typer.Option(True, "-l/-1", "--long/--one", help="Show detailed list"),
          config: bool = typer.Option(False, "-c", "--config", help="include configuration section"),
          files: bool = typer.Option(False, "-f", "--files", help="show installed files")):
    """
    List the configured projects and their state.
    """
    all_projects = edit_projects()
    if details:
        table = Table(box=None)
        table.add_column('Project')
        table.add_column('Installed')
        table.add_column('Candidate')
        table.add_column('Updated')
        if config:
            table.add_column('Config')
        if files:
            table.add_column('Files')
        for name in all_projects:
            if projects and name not in projects:
                continue
            try:
                project = get_project(name)
                cells = [name, '?', repr(project.select_release()), '?']
                if config:
                    cells.append(Syntax(tomlkit.dumps({name: project.config}), 'toml'))
                if files:
                    cells.append(file_table(project))
                table.add_row(*cells)
            except Exception as e:
                logger.error('Project %s could not be created: %s (config: %s)', name, e, all_projects[name])
        console.print(table)
    else:
        console.print("\n".join(name for name in all_projects))


@app.command()
def edit():
    """
    Edit the project configuration file.
    """
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi'
    editor_cmd = shutil.which(editor)
    projects = edit_projects()
    last_modified = projects.store.stat().st_mtime
    subprocess.run([editor_cmd, os.fspath(projects.store)])
    if projects.store.stat().st_mtime > last_modified:
        projects.load()
        list_()

@app.command()
def pd(project_name: str, command: List[str] = typer.Argument(None, help="Command to run in the project directory")):
    """
    Access the given project and run a command in its directory. If no command is given, just print the project directory.
    """
    try:
        project = get_project(project_name, must_exist=True)
        if not command:
            console.print(os.fspath(project.directory))
        else:
                with project.use_directory():
                    result = subprocess.run(command)
                    sys.exit(result.returncode)
    except Exception as e:
        logger.error('Failed to run command %s for project %s: %s', ' '.join(command) or 'pd', project_name, e)

class NoPatternError(ValueError):
    ...


def identifying_pattern(alternatives: list[str], selection: str, version: Optional[str] = None) -> str:
    """
    Given a selection string and a set of alternatives, this function returns a version of selection 
    that replaces all substrings common to all the selection and all alternatives with a '*'. E.g.,
    
    >>> identifying_pattern(['foo-windows.tar.gz', 'foo-macos.tar.gz'],'foo-linux.tar.gz')
    '*linux*'
    """
    if selection in alternatives:
        alternatives = [a for a in alternatives if selection != a]

    def check_pattern(pattern, exception=True):
        """
        A pattern is valid if it matches the selection but not any of the alternatives.
        """
        try:
            if not fnmatch.fnmatch(selection, pattern):
                raise NoPatternError(f'Could not generate a match pattern. The candidate, "{pattern}", does not match "{selection}".')
            matching_alternatives = fnmatch.filter(alternatives, pattern)
            if matching_alternatives:
                raise NoPatternError(f'Could not generate a match pattern. The candidate, "{pattern}", matches {len(matching_alternatives)} alternatives: {matching_alternatives}')
            logger.debug('Pattern %s for selection %s, alternatives %s', pattern, selection, alternatives)
            return True
        except NoPatternError as e:
            logger.debug(e)
            if exception:
                raise
            else:
                return False

    if version:
        version_pattern = re.sub(r'\W', '.', version)
        if version_pattern[0].casefold() == 'v':
            version_pattern = version_pattern[1:]
        version_pattern = '[vV]?' + version_pattern
        versionless = re.sub(version_pattern, '*', selection)
        if check_pattern(versionless, exception=False):
            return versionless

    # try some typical constructions
    #     path = Path(selection)
    #     dir_star = str(Path('*', path.name))
    #     if check_pattern(dir_star):
    #         return dir_star
    #     name_star = str(path.with_name('*'))
    #     if check_pattern(name_star):
    #         return name_star
    #     stem_star = str(path.with_stem('*'))
    #     if stem_star != name_star and check_pattern(stem_star):
    #         return stem_star

    # try minimal substrings
    substring = unique_substrings([selection] + alternatives).get(selection)
    if substring:
        pos = selection.index(substring)
        result = ''
        if pos == 0:
            result += '*'
        result += substring
        if pos + len(substring) < len(selection):
            result += '*'
        return result

    # collect common substrings (or rather, character indexes)
    common_idx = set(range(len(selection)))
    for alternative in alternatives:
        matcher = SequenceMatcher(a=selection, b=alternative)
        matching_idx = set(chain.from_iterable(range(m.a, m.a+m.size) for m in matcher.get_matching_blocks()))        
        common_idx &= matching_idx        
    
    # create pattern from that
    pattern_parts =  []
    for i in range(len(selection)):
        if i in common_idx:
            if i-1 not in common_idx:
                pattern_parts.append('*')
        else:
            pattern_parts.append(selection[i])
    pattern = ''.join(pattern_parts)
    
    # assert correctness

    return pattern
