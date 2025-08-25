import logging
import shlex
import tarfile
from abc import abstractmethod
from collections.abc import Callable, Iterable
from datetime import date
from fnmatch import fnmatch
from ntpath import relpath
from os import fspath
from pathlib import Path
from sys import argv
from tempfile import NamedTemporaryFile
from typing import overload
from zipfile import BadZipFile, ZipFile

import msgspec

from getrel.utils import expand

logger = logging.getLogger(__name__)


class ConfigError(ValueError): ...


def _actiontag(classname: str):
    return classname.removesuffix("Action").lower()


class Action(msgspec.Struct, tag=_actiontag, tag_field="action", omit_defaults=True):
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

    destination: str | None = None
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
    link: str | None = None
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
    """
    Creates a symbolic link to each of the files at the directory or filename given by the link property.
    """

    def __call__(self, project_files: list[Path]) -> None:
        link_dir = self._prepare_linkdir(project_files)
        for source in self.sources:
            self._create_link(source, link_dir, project_files)


class BinAction(AbstractLinkAction):
    """
    Creates a symbolic link for each of the binaries listed as source.
    """

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


class ScriptAction(Action):
    """
    Runs the given command or script.
    """

    cmd: str | None = None
    """A single command with its arguments."""

    script: str | None = None  # FIXME mutually exclusive
    """Either a script starting with a #! line, or a shell command that is run with the current default shell."""

    creates: list[str] | None = None
    """Optional list of files this action may create."""

    # FIXME: use logproc

    def __call__(self, project_files: list[Path]) -> None:
        if self.cmd:
            self._run_cmd(self.cmd, project_files, shell=False)
        elif self.script is not None and self.script.strip().startswith("#!"):
            self._run_script(self.script, project_files)
        else:
            assert self.script is not None  # guaranteed by validation
            self._run_cmd(self.script, project_files, shell=True)

    def _run_cmd(self, cmd: str, project_files: list[Path], shell: bool = False):
        capture_stdout = self.creates is None
        if shell:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE if capture_stdout else None,
                text=True,
                shell=True,
            )
        else:
            args = shlex.split(cmd)
            executable = shutil.which(args[0])
            if executable is None:
                raise ConfigError(
                    'Executable %s not found for command "%s"', args[0], cmd
                )
            args[0] = executable
            process = subprocess.Popen(
                args, stdout=subprocess.PIPE if capture_stdout else None, text=True
            )
        stdout, _ = process.communicate()
        project_files.extend(Path(line) for line in stdout.splitlines() if line)

    def _run_script(self, script: str, project_files: list[Path]):
        with NamedTemporaryFile(
            "wt", prefix="getrel", delete_on_close=False
        ) as script_file:
            script_file.write(script.strip() + "\n")
            script_file.close()
            script_path = Path(script_file.name)
            script_path.chmod(0o700)
            self._run_cmd(shlex.quote(fspath(script_path)), project_files)


class Project(msgspec.Struct, omit_defaults=True):
    url: str
    actions: list[UnpackAction | BinAction | LinkAction | ScriptAction]


if __name__ == "__main__":
    if len(argv) < 2:
        schema = msgspec.json.schema(Project)
        Path("getrel-project.schema.json").write_bytes(msgspec.json.encode(schema))
    elif len(argv) == 2:
        struct = msgspec.toml.decode(
            Path(argv[1]).read_text(encoding="utf-8"), type=Project
        )
        print(struct)
        print(msgspec.json.encode(struct))
