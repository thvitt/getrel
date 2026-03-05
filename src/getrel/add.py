import fnmatch
import logging
import re
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from importlib.resources import read_binary
from itertools import chain
from typing import Literal, Self

import msgspec
from httpx import Client
from msgspec import Struct
from rich.progress import Progress

from getrel.github import Asset, GithubProjectManager
from getrel.utils import WorkingDirectory, first

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

    def rate(self, asset: Asset):
        if self.name and not fnmatch.fnmatch(asset.name, self.name):
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

    def rate(self, rules: list[Relevance], what: Asset) -> float:
        return sum(rule.rate(what) for rule in rules)


class ScoredAsset(Struct):
    asset: Asset
    score: float

    @classmethod
    def score_assets(cls, assets: Iterable[Asset], scorer: PreferenceScores):
        scores = [cls(asset, scorer.rate(scorer.assets, asset)) for asset in assets]
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
            if not fnmatch.fnmatch(selection, pattern):
                raise NoPatternError(
                    f'Could not generate a match pattern. The candidate, "{pattern}", does not match "{selection}".'
                )
            matching_alternatives = fnmatch.filter(alternatives, pattern)
            if matching_alternatives:
                raise NoPatternError(
                    f'Could not generate a match pattern. The candidate, "{pattern}", matches {len(matching_alternatives)} alternatives: {matching_alternatives}'
                )
            logger.debug(
                "Pattern %s for selection %s, alternatives %s",
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

    if version:
        versionless = mask_version(selection, version)
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

    if not avoid_minimal:
        # try minimal substrings
        substring = unique_substrings([selection, *alternatives]).get(selection)
        if substring:
            pos = selection.index(substring)
            result = ""
            if pos == 0:
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


def add(url: str, auto_level: Literal[0, 1, 2] = 0):
    scorer = PreferenceScores.load()
    manager = GithubProjectManager()
    project, state, assets_ = manager.prepare_project(url)
    assets = ScoredAsset.score_assets(assets_, scorer)
    top = _top_scored(assets, key=lambda s: s.score)
    # TODO: Interactivity
    project.download = [
        identifying_pattern(
            [a.asset.name for a in assets],
            assets[0].asset.name,
            state.available.version if state.available else None,
        )
    ]
    with (
        WorkingDirectory(state.project_dir),
        Client() as client,
        Progress() as progress,
    ):
        file_idx = len(state.installed_files)
        manager.download(project, assets_, client, progress)

        # Here are the rules:
        # - archives -> unpackaction
        # -
