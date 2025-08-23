import logging
import tarfile
from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Iterable
from fnmatch import fnmatch
from nt import link
from ntpath import relpath
from os import chdir, fspath
from os.path import expanduser, expandvars
from pathlib import Path
from typing import Literal, overload
from zipfile import BadZipFile, ZipFile

from _pytest._code import source
from _typeshed import StrPath
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ConfigError(ValueError): ...


def expand(src: StrPath):
    return Path(expandvars(src)).expanduser()


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
    """
    Run some action (specified by the value of the _action_ field) on the source.

    Concrete actions must implement the __call__ method. The actions are run directly
    inside the project directory, so relative paths etc. will be relative to the project directory.
    """

    source: str
    """
    Shell glob pattern (or fixed string) specifying the sources the action will work on.
    Usually this will be relative to the project directory.
    """

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
            path = expand(self.source)
            if path.is_absolute():
                return path.parent.glob(path.name)
            else:
                return Path().glob(self.source)
        else:
            return [path for path in candidates if fnmatch(str(path), self.source)]

    @property
    def sources(self) -> list[Path]:
        return list(self.expand_source())


class UnpackAction(Action):
    """
    Unpacks the archive(s) at source to the destination or current (project) directory.

    The action supports zip and tar files, and transparently handles tar compression.

    See also:
        zipfile, tarfile
    """

    action: Literal["unpack"] = "unpack"

    destination: Path | None = None
    """The path to which to unpack. If missing or None, unpack in the current (project) directory."""

    delete_source: bool = False
    """If true, delete the archive after successful extraction."""

    def _make_record_tar_filter(
        self, recorder: list[Path]
    ) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]:
        def _filter(member: tarfile.TarInfo, path: str, /) -> tarfile.TarInfo | None:
            info = tarfile.data_filter(member, path)
            if info is not None:
                recorder.append((Path(path) / info.name).absolute())
            return info

        return _filter

    def __call__(self, project_files: list[Path]) -> None:
        for source in self.sources:
            try:
                with ZipFile(source) as archive:
                    members = [
                        name
                        for name in archive.namelist()
                        if name is not None
                        and not (name.startswith("/") or name.startswith("../"))
                    ]
                    archive.extractall(self.destination, members)
                    dest = expand(self.destination or Path.cwd())
                    project_files.extend(
                        dest.joinpath(member).absolute() for member in members
                    )
            except BadZipFile as zip_error:
                try:
                    with tarfile.open(source) as archive:
                        archive.extractall(
                            fspath(self.destination or "."),
                            filter=self._make_record_tar_filter(project_files),
                        )
                except tarfile.ReadError as tar_error:
                    logger.error(
                        "Failed to unpack %s: %s and %s", source, zip_error, tar_error
                    )


class AbstractLinkAction(Action):
    link: str | None
    dir: bool = False
    absolute: bool = False

    def _prepare_linkdir(self, project_files: list[Path]) -> Path:
        if self.link is None:
            raise ConfigError(
                "link is missing for link action with source pattern %s: Do not know where to link to.",
                self.source,
            )
        link = expand(self.link)
        if self.link.endswith("/") or link.is_dir() or self.dir:
            link_dir = link
        else:
            link_dir = link.parent
            if len(self.sources) > 1:
                raise ConfigError(
                    "source %s expands to multiple files (%s), but link %s is not a directory",
                    self.source,
                    self.sources,
                    self.link,
                )

        if not link_dir.exists():
            link_dir.mkdir(parents=True)
            project_files.append(link_dir)

        return link_dir

    def _create_link(self, source: Path, target: Path, project_files: list[Path]):
        """
        Creates a single link to source.

        Args:
            source: The file the link will point to
            target: Either an existing directory in which source will be linked with the same name,
                    or the direct path to the file to create
        """
        if target.is_dir():
            link_dir = target
            link_path = link_dir / source.name
        else:
            link_dir = target.parent
            link_path = target
        if self.absolute:
            final_path = source.absolute()
        else:
            final_path = relpath(source, link_path.parent)
        if link_path.exists():
            if link_path.is_symlink():
                if link_path.readlink().samefile(final_path):
                    logger.info(
                        "Recreating symbolic link %s to %s", link_path, final_path
                    )
                else:
                    logger.warning(
                        "Creating symbolic link %s to %s, overwriting existing link to %s",
                        link_path,
                        final_path,
                        link_path.readlink(),
                    )
            else:  # no symlink
                logger.warning(
                    "Overwriting regular file %s with a symbolic link to %s",
                    link_path,
                    final_path,
                )
        else:
            logger.debug("Creating symbolic link %s to %s", link_path, final_path)
        link_path.symlink_to(final_path)
        project_files.append(link_path)


class LinkAction(AbstractLinkAction):
    action: Literal["link"] = "link"

    def __call__(self, project_files: list[Path]) -> None:
        link_dir = self._prepare_linkdir(project_files)
        for source in self.sources:
            self._create_link(source, link_dir, project_files)


class BinAction(AbstractLinkAction):
    action: Literal["bin"] = "bin"
    link: str | None
    bin: str | None = None

    def __call__(self, project_files: list[Path]) -> None:
        if self.link is None:
            self.link = "~/.local/bin/"
        link_dir = self._prepare_linkdir(project_files)
        bin_ = None if self.bin is None else expand(self.bin)
        for source in self.sources:
            if bin_ is None:
                self._create_link(source, link_dir, project_files)
            elif bin_.is_absolute():
                self._create_link(source, bin_, project_files)
            else:
                self._create_link(source, link_dir / bin_, project_files)
