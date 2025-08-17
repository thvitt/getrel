import logging
import tarfile
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from fnmatch import fnmatch
from os import chdir, fspath
from pathlib import Path
from typing import Literal, overload
from zipfile import BadZipFile, ZipFile

from _pytest._code import source
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class WorkingDirectory:
    directory: Path
    previous: Path | None = None

    def __init__(self, directory: Path, create: bool = True) -> None:
        if create and not directory.exists():
            directory.mkdir(parents=True)
        self.directory = directory

    def __enter__(self):
        self.previous = Path.cwd()
        chdir(self.directory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        assert self.previous is not None
        chdir(self.previous)
        return False


class Action(BaseModel, metaclass=ABCMeta):
    source: str

    @abstractmethod
    def __call__(self, project_files: list[Path]) -> None:
        """
        Runs the current action.

        Args:
            project_files: the files managed by the project.
            This method modifies the list by adding the files
            it creates and removing the files it deletes.
        """

    @overload
    def expand_source(self, candidates: Iterable[str]) -> Iterable[str]: ...

    @overload
    def expand_source(
        self, candidates: Iterable[Path] | None = None
    ) -> Iterable[Path]: ...

    def expand_source(
        self, candidates: Iterable[Path | str] | None = None
    ) -> Iterable[Path | str]:
        """
        Expands the action’s source field either as glob pattern of the current directory.
        If a list of candidates is given, the list is filtered against the pattern instead.

        Args:
            candidates: Optional list of paths or strings to filter against the pattern.

            Args:
                candidates: Optional list of paths or strings to filter against the pattern.
        """  # noqa: RUF002
        if candidates is None:
            path = Path(self.source)
            if path.is_absolute():
                return path.parent.glob(path.name)
            else:
                return Path().glob(self.source)
        else:
            return [path for path in candidates if fnmatch(str(path), self.source)]


class UnpackAction(Action):
    action: Literal["unpack"] = "unpack"
    destination: Path | None = None
    delete_source: bool = False

    def __call__(self, project_files: list[Path]) -> None:
        for source in self.expand_source():
            try:
                with ZipFile(source) as archive:
                    archive.extractall(self.destination)  # TODO Safety check
            except BadZipFile as zip_error:
                try:
                    with tarfile.open(source) as archive:
                        archive.extractall(fspath(self.destination or "."))
                except tarfile.ReadError as tar_error:
                    logger.error(
                        "Failed to unpack %s: %s and %s", source, zip_error, tar_error
                    )


class LinkAction(Action):
    action: Literal["link"] = "link"
    link: str
    dir: bool = False
    absolute: bool = False

    def __call__(self, project_files: list[Path]) -> None:
        sources = list(self.expand_source())
        link = Path(self.link)
        if self.link.endswith("/") or link.is_dir() or self.dir:
            link_dir = link
        else:
            link_dir = link.parent
            # TODO: check number of arguments

        if not link_dir.exists():
            link_dir.mkdir(parents=True)
            project_files.append(link_dir)

        for source in sources:
            link_path = link_dir / source.name
            # TODO check existence
            # TODO relativize / absolute
            link_path.symlink_to(source)
            project_files.append(link_path)
