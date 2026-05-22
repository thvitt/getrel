import fnmatch
import logging
import os
import re
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from importlib.resources import read_binary
from itertools import chain
from pathlib import Path
from platform import machine
from typing import Literal, Self

import msgspec
from httpx import Client
from msgspec import Struct
from rich.progress import Progress

from getrel.actions import BinAction, LinkAction, Settings, UnpackAction
from getrel.config import save_state
from getrel.github import Asset, GithubProjectManager
from getrel.utils import FileType, WorkingDirectory, field_names, first, unique

logger = logging.getLogger(__name__)


class NoPatternError(ValueError): ...


class Relevance(Struct, omit_defaults=True):
    """
    A relevance rule.

    If either name or mime are present, use them as glob patterns and match them against the
    relevant item. If they match, the given score is added. If both are present, both must match.
    """

    score: float
    name: str = ""
    mime: str = ""

    def rate(self, asset: Asset, settings: Settings):
        if self.name:
            patterns = settings.expand_arch(self.name)
            if not any(fnmatch.fnmatch(asset.name, p) for p in patterns):
                return 0
        if self.mime and not fnmatch.fnmatch(asset.contentType, self.mime):
            return 0
        if self.name or self.mime:
            return self.score
        return 0


class PreferenceScores(Struct, omit_defaults=True):
    assets: list[Relevance]

    @classmethod
    def load(cls) -> Self:
        return msgspec.yaml.decode(read_binary(__name__, "scores.yaml"), type=cls)

    def rate(self, rules: list[Relevance], what: Asset, settings: Settings) -> float:
        return sum(rule.rate(what, settings) for rule in rules)


class ScoredAsset(Struct):
    asset: Asset
    score: float

    @classmethod
    def score_assets(
        cls, assets: Iterable[Asset], scorer: PreferenceScores, settings: Settings
    ):
        scores = [
            cls(asset, scorer.rate(scorer.assets, asset, settings)) for asset in assets
        ]
        return sorted(
            scores, key=lambda a: (a.score, a.asset.downloadCount), reverse=True
        )


def _top_scored[T](sorted_items: Iterable[T], key: Callable[[T], float]) -> list[T]:
    it = iter(sorted_items)
    result = [next(it)]
    reference = key(result[0])
    for item in it:
        if key(item) >= reference:
            result.append(item)
        else:
            break
    return result


def identifying_pattern(
    alternatives: list[str],
    selection: str,
    version: str | None = None,
    settings: Settings | None = None,
    avoid_minimal=False,
) -> str:
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
            patterns = [pattern]
            if settings:
                patterns = list(settings.expand_arch(pattern))

            if not any(fnmatch.fnmatch(selection, p) for p in patterns):
                raise NoPatternError(
                    f'Could not generate a match pattern. The candidate, "{pattern}", does not match "{selection}".'
                )
            for p in patterns:
                matching_alternatives = fnmatch.filter(alternatives, p)
                if matching_alternatives:
                    raise NoPatternError(
                        f'Could not generate a match pattern. The candidate, "{pattern}" (as "{p}"), matches {len(matching_alternatives)} alternatives: {matching_alternatives}'
                    )
            logger.debug(
                "Pattern %s matches selection %s, but not alternatives %s",
                pattern,
                selection,
                alternatives,
            )
            return True
        except NoPatternError as e:
            logger.debug(e)
            if exception:
                raise
            else:
                return False

    pattern = selection
    if version:
        pattern = mask_version(pattern, version)
    if settings:
        pattern = mask_architecture(pattern, settings)

    if pattern != selection and check_pattern(pattern, exception=False):
        return pattern

    if not avoid_minimal:
        # try minimal substrings
        substring = unique_substrings([selection, *alternatives]).get(selection)
        if substring:
            pos = selection.index(substring)
            result = ""
            if pos != 0:
                result += "*"
            result += substring
            if pos + len(substring) < len(selection):
                result += "*"
            return result

    # collect common substrings (or rather, character indexes)
    common_idx = set(range(len(selection)))
    for alternative in alternatives:
        matcher = SequenceMatcher(a=selection, b=alternative)
        matching_idx = set(
            chain.from_iterable(
                range(m.a, m.a + m.size) for m in matcher.get_matching_blocks()
            )
        )
        common_idx &= matching_idx

    # create pattern from that
    pattern_parts = []
    for i in range(len(selection)):
        if i in common_idx:
            if i - 1 not in common_idx:
                pattern_parts.append("*")
        else:
            pattern_parts.append(selection[i])
    pattern = "".join(pattern_parts)

    # assert correctness
    if version:
        pattern = mask_version(pattern, version)
    if settings:
        pattern = mask_architecture(pattern, settings)

    return pattern


def unique_substrings(strings: Iterable[str]) -> dict[str, str]:
    """
    Maps each given string to the shortest substring identifying the string within the list.

    Returns a mapping string:substring for each string for which a shortest identifying usbstring has been found.

    Example:
        >>> unique_substrings(['ab', 'abab', 'abc'])
        {'abab': 'ba', 'abc': 'c'}

    Note that 'ab' is not in the results since it is completely contained within 'abab'
    """
    candidates = {}  # Map substring -> None if not unique  | string for identified string
    for string in strings:
        for length in range(1, len(string)):
            for start in range(0, len(string) - length + 1):
                substr = string[start : start + length]
                candidates[substr] = None if substr in candidates else string

    unique = {}  # Map string -> shortest identifying substring
    for candidate, string in candidates.items():
        if string is not None:  # noqa: SIM102
            if string not in unique or len(unique[string]) > len(candidate):
                unique[string] = candidate
    return unique


def mask_version(name, version):
    version_pattern = re.sub(r"\W", ".", version)
    if version_pattern[0].casefold() == "v":
        version_pattern = version_pattern[1:]
    version_pattern = "[vV]?" + version_pattern
    versionless = re.sub(version_pattern, "*", name)
    return versionless


def mask_architecture(name: str, settings: Settings) -> str:
    arch = machine()
    aliases = [arch, *settings.architectures.get(arch, [])]
    for a in sorted(aliases, key=len, reverse=True):
        name = name.replace(a, "{arch}")
    return name


def add(url: str, auto_level: Literal[0, 1, 2] = 0, prerelease: bool = False):
    scorer = PreferenceScores.load()
    settings = Settings.load()
    manager = GithubProjectManager()
    project, state, assets_ = manager.prepare_project(url, prerelease=prerelease)
    logger.debug("Project: %s, State: %s, Assets: %s", project, state, assets_)
    assets = ScoredAsset.score_assets(assets_, scorer, settings)
    top = _top_scored(assets, key=lambda s: s.score)
    logger.debug("Top assets: %s", top)
    project.download = [
        identifying_pattern(
            [a.asset.name for a in top],
            assets[0].asset.name,
            state.available.version if state.available else None,
            settings=settings,
        )
    ]
    with (
        WorkingDirectory(state.project_dir),
        Client() as client,
        Progress() as progress,
    ):
        file_idx = len(state.installed_files)
        downloaded_files = list(manager.download(project, assets_, client, progress))
        logger.debug(
            "Downloaded files: %s, from asset list: %s", downloaded_files, assets_
        )

        new_files = list(downloaded_files)
        binary_limit = 5
        binary_count = 0

        while new_files:
            current_files = new_files
            new_files = []

            for file in current_files:
                action = None
                file_path = Path(file)
                try:
                    rel_path = file_path.relative_to(Path.cwd())
                except ValueError:
                    rel_path = file_path

                logger.debug("Analyzing %s ...", rel_path)
                filetype = FileType(rel_path)

                for rule in settings.add_rules:
                    if rule.matches and not any(
                        fnmatch.fnmatch(str(rel_path), p) for p in rule.matches
                    ):
                        continue
                    if rule.mime and not any(
                        fnmatch.fnmatch(filetype.mime or "", p) for p in rule.mime
                    ):
                        continue
                    if rule.exclude and any(
                        fnmatch.fnmatch(str(rel_path), p) for p in rule.exclude
                    ):
                        continue
                    action = rule.then
                    if action == "skip":
                        action = None
                    break

                if action is None:
                    if filetype.archive:
                        action = UnpackAction(source=str(rel_path))
                    elif (
                        file_path.name.startswith("_") or "completions" in rel_path.parts
                    ):
                        action = LinkAction(
                            source=str(rel_path), link="~/.zsh/completions"
                        )
                    elif file_path.name.endswith(".1"):
                        action = LinkAction(
                            source=str(rel_path), link="~/.local/man/man1"
                        )
                    elif filetype.executable:
                        if binary_count < binary_limit:
                            action = BinAction(source=str(rel_path))
                            binary_count += 1
                        else:
                            logger.warning(
                                "Sanity limit reached: skipping BinAction for %s",
                                file_path.name,
                            )

                if action:
                    if state.available and state.available.version:
                        action.source = mask_version(
                            action.source, state.available.version
                        )
                    action.source = mask_architecture(action.source, settings)
                    logger.debug("Added %s for %s", action, file)
                    project.install.append(action)
                    action_created_files = []  # FIXME should pass list of all files
                    action(action_created_files)
                    logger.debug("… created files: %s", action_created_files)

                    if state.installed_files is None:
                        state.installed_files = []

                    for new_f in action_created_files:
                        state.installed_files.append(new_f)
                        if new_f.is_relative_to(Path.cwd()):
                            new_files.append(new_f)
                else:
                    logger.debug("No suitable action for %s", file)
            config_yaml = project.save()
            logger.info("Final project config:\n%s", config_yaml.decode())
            logger.debug("Final project state: %s", state)
            save_state(manager.states)
