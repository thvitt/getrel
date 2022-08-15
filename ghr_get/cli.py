from collections.abc import Mapping
from typing import Collection, Optional
from prompt_toolkit.document import Document
from prompt_toolkit.validation import ValidationError, Validator
import typer
from .project import GitHubProject, GithubAsset
import questionary
from difflib import SequenceMatcher
import fnmatch
from itertools import chain
from .utils import FileType, first
from .config import edit_projects

import logging
from rich.logging import RichHandler
from rich.console import Console
from rich.syntax import Syntax
import tomlkit

console = Console()

FORMAT = "%(message)s"
logging.basicConfig(
    level="NOTSET", format=FORMAT, datefmt="[%X]", handlers=[RichHandler(console=console)]
)
logger = logging.getLogger(__name__)

app = typer.Typer()

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
    return questionary.Choice(str(asset), value=asset)


@app.command()
def add(url: str):
    """
    Interactively install a tool and add its configuration.
    """
    with edit_projects() as settings:
        project = GitHubProject.from_url(url)
        project.update()
        if 'release' not in project.config:
            special = dict(
                latest = first((release for release in project.releases if not release['prerelease'] and not release['draft']), default=None),
                pre = first((release for release in project.releases if not release['draft']), default=None))
            choices = []
            for label, release in special.items():
                if release:
                    choices.append(rel2choice(release, label))
            choices.extend(rel2choice(r) for r in project.releases)
            selected = questionary.select('Which release would you like to use', choices, 
                                            use_arrow_keys=True, use_shortcuts=True).ask()
            logger.debug('Selected release %s', selected)
            if selected in {'latest', 'pre'}:
                project.config['release'] = selected
                release_record = special[selected]
            else:
                release_record = [p for p in project.releases if p['tag_name'] == selected][0]
                try:
                    pattern = identifying_pattern(selected, [r['tag_name'] for r in project.releases])
                    if '*' in pattern:
                        pattern = questionary.text(f'Release matching pattern', 
                                default=pattern, 
                                validate=FNMatchValidator([c.value for c in choices], must_match=selected)).ask()
                except NoPatternError:
                    pattern = selected
                project.config['release'] = pattern
        else:
            release_record = project.select_release()

        logger.debug('Project config: %s', project.config)
        logger.debug('Global settings:\n%s', settings)

        # now select the asset(s)
        asset_choices = [asset2choice(a) for a in project.get_assets(release=release_record, configured=False)]
        selected_assets = questionary.checkbox('Which asset(s) should be downloaded?', asset_choices).ask()

        for asset in selected_assets:
            asset_name = asset.source.name
            console.rule(asset_name)
            asset_names = [a['name'] for a in release_record['assets']]
            try:
                pattern = identifying_pattern(asset_name, asset_names)
            except NoPatternError:
                pattern = asset_name

            pattern = questionary.text(f'Asset matching pattern', 
                    default=pattern, 
                    validate=FNMatchValidator(asset_names, must_match=asset_name, max_matches=1),
                    instruction=f'edit/accept pattern for {asset_name}').ask()
            logger.info('Asset pattern: %s', pattern)
            asset.configure({'match': pattern})
            asset.download()
            # now let’s see what we got 
            kind = FileType(asset.source)
            if kind.executable:
                if questionary.confirm(f'{asset.source.name} seems to be an executable ({kind.description}). '
                        'Should I install it as binary {project.name}?').ask():
                    asset.configure({'install': 'bin'})
                else:
                    bin = questionary.text('Use a different binary name?', instruction='Enter name relative to {Path.home() / ".local/bin"} or leave empty if it should not be installed').ask()
                    if bin:
                        asset.configure({'install': {'bin': bin}})
            if not asset.spec.get('install') and kind.archive:
                if questionary.confirm(f'{asset.source.name} is an archive ({kind.description}). '
                        'Should I unpack it?').ask():
                    asset.configure({'install': 'unpack'})
            if not asset.spec.get('install'):
                link = questionary.path('Symlink somewhere? You can use ~ and ${VAR}s', only_directories=True).ask()
                if link:
                    asset.configure({'install': {'link': link}})
            logger.debug('Running install for spec %s', asset.spec)
            asset.install()



@app.command('list')
def list_():
    projects = edit_projects()
    for name, project_config in projects.items():
        console.print(Syntax(tomlkit.dumps({name: project_config}), 'toml'))


class NoPatternError(ValueError):
    ...


def identifying_pattern(selection: str, alternatives: list[str]) -> str:
    """
    Given a selection string and a set of alternatives, this function returns a version of selection 
    that replaces all substrings common to all the selection and all alternatives with a '*'. E.g.,
    
    >>> get_pattern('foo-linux.tar.gz', ['foo-windows.tar.gz', 'foo-macos.tar.gz'])
    '*linux*'
    """
    # collect common substrings (or rather, character indexes)
    if selection in alternatives:
        alternatives = [a for a in alternatives if selection != a]
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
    if not fnmatch.fnmatch(selection, pattern):
        raise NoPatternError(f'Could not generate a match pattern. The candidate, "{pattern}", does not match "{selection}".')
    matching_alternatives = fnmatch.filter(alternatives, pattern)
    if matching_alternatives:
        raise NoPatternError(f'Could not generate a match pattern. The candidate, "{pattern}", matches {len(matching_alternatives)} alternatives: {matching_alternatives}')
        
    return pattern
