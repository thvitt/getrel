from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tarfile
from abc import abstractmethod
from datetime import datetime
from fnmatch import fnmatch
from functools import lru_cache
from os import fspath
from os.path import expandvars
from pathlib import Path
from platform import machine
from stat import S_IXGRP, S_IXOTH, S_IXUSR
from sys import argv
from tempfile import NamedTemporaryFile
from textwrap import indent
from typing import TYPE_CHECKING, Literal, Self, overload
from zipfile import BadZipFile, ZipFile

import msgspec
import xdg.BaseDirectory
from logproc import execute
from rich.text import Text
from rich.tree import Tree

from getrel.utils import FileType, WorkingDirectory, field_names, first, unique

if TYPE_CHECKING:
    from collections.abc import Callable, Container, Iterable

logger = logging.getLogger(__name__)

DATA_DIR = Path(xdg.BaseDirectory.xdg_data_home, "getrel")


class ConfigError(ValueError): ...


def _actiontag(classname: str):
    return classname.removesuffix("Action").lower()


class BaseAction(
    msgspec.Struct, tag=_actiontag, tag_field="action", omit_defaults=True, kw_only=True
):
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
        result = []
        for path in Settings.load().expand(self.source):
            if candidates is None:
                if path.is_absolute():
                    result.extend(path.parent.glob(path.name))
                    logger.debug(" ... abs: %s ~> %s", path, result)
                else:
                    result.extend(Path().glob(fspath(path)))
                    logger.debug(" ... rel: %s ~> %s", path, result)
            else:
                result.extend(
                    cand for cand in candidates if fnmatch(str(cand), str(path))
                )
        return unique(result)

    @property
    def sources(self) -> list[Path]:
        result = list(self.expand_source())
        logger.debug(
            "%s: sources %s ~> %s (in %s)",
            self.__class__.__name__,
            self.source,
            result,
            Path().absolute(),
        )
        return result


class UnpackAction(BaseAction):
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
                    dests = Settings.load().expand(self.destination or Path.cwd())
                    project_files.extend(
                        dest.joinpath(member).absolute()
                        for member in members
                        for dest in dests
                    )
            except BadZipFile as zip_error:
                try:
                    with tarfile.open(source) as archive:
                        archive.extractall(
                            fspath(self.destination or "."),
                            filter=self._make_record_tar_filter(project_files),
                        )
                except tarfile.ReadError as tar_error:
                    if source.suffix == ".zst" or source.name.endswith(".tar.zst"):
                        try:
                            with (
                                subprocess.Popen(["zstd", "-dc", fspath(source)], stdout=subprocess.PIPE) as proc,
                                tarfile.open(fileobj=proc.stdout, mode="r|") as archive,
                            ):
                                    archive.extractall(
                                        fspath(self.destination or "."),
                                        filter=self._make_record_tar_filter(project_files),
                                    )
                            continue
                        except Exception as zstd_error:
                             logger.error("Failed to unpack %s with zstd: %s", source, zstd_error)
                    logger.error(
                        "Failed to unpack %s: %s and %s", source, zip_error, tar_error
                    )

    def __str__(self) -> str:
        return f"unpack the archive `{self.source}` to `{self.destination or 'the project directory'}`"


class AbstractLinkAction(BaseAction):
    link: str | None = None
    dir: bool = False
    absolute: bool = False

    def _prepare_linkdir(self, project_files: list[Path]) -> Path:
        if self.link is None:
            raise ConfigError(
                "link is missing for link action with source pattern %s: Do not know where to link to.",
                self.source,
            )
        link = first(Settings.load().expand(self.link))
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
            final_path = Path(os.path.relpath(source, link_path.parent))
        if link_path.exists():
            if link_path.is_symlink():
                if link_path.readlink().resolve() == final_path.resolve():
                    logger.info(
                        "Recreating symbolic link %s to %s", link_path, final_path
                    )
                else:
                    level = (
                        logging.INFO
                        if final_path.readlink()
                        .resolve()
                        .is_relative_to(Path().absolute())
                        else logging.WARNING
                    )
                    logger.log(
                        level,
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
        link_path.unlink(missing_ok=True)
        link_path.symlink_to(final_path)
        project_files.append(link_path)


class LinkAction(AbstractLinkAction):
    """
    Creates a symbolic link to each of the files at the directory or filename given by the link property.

    Use `dir = true` to forcibly interprete `destination` as a directory, use `absolute = true` to create a link to an absolute path.
    """

    def __call__(self, project_files: list[Path]) -> None:
        link_dir = self._prepare_linkdir(project_files)
        for source in self.sources:
            self._create_link(source, link_dir, project_files)

    def __str__(self) -> str:
        return f"create {'an absolute' if self.absolute else 'a'} symbolic link to `{self.source}` at `{self.link}`"


class BinAction(AbstractLinkAction):
    """
    Creates a symbolic link for each of the binaries listed as source.

    This sets execute permissions for the source.
    If `bin` is given, the binary's file name can be forced.
    If `link` is not given, `~/.local/bin/` is used.
    """

    bin: str | None = None

    def __call__(self, project_files: list[Path]) -> None:
        if self.link is None:
            self.link = "~/.local/bin/"
        link_dir = self._prepare_linkdir(project_files)
        bin_ = None if self.bin is None else first(Settings.load().expand(self.bin))
        for source in self.sources:
            source.chmod(source.stat().st_mode | S_IXUSR | S_IXGRP | S_IXOTH)
            if bin_ is None:
                self._create_link(source, link_dir, project_files)
            elif bin_.is_absolute():
                self._create_link(source, bin_, project_files)
            else:
                self._create_link(source, link_dir / bin_, project_files)

    def __str__(self) -> str:
        return f"link the binary or binaries matching `{self.source}`"


def path_recorder(files: list[Path]) -> Callable[[str | bytes], None]:
    def recorder(line: str | bytes) -> None:
        if isinstance(line, bytes):
            line = line.decode()
        line = line.removesuffix("\n")
        if line:
            files.append(Path(line))

    return recorder


class ScriptAction(BaseAction):
    """
    Runs the given command or script.

    source is ignored. For `cmd`, the string is interpreted as a executable with arguments.
    For `script`, you can either give a single command (that is run using the shell), or a
    multi-line script beginning with a `#!` line. A script would be saved to a temporary file
    for execution.

    The execution happens in the projct directory.
    """

    source: str = ""

    cmd: str | None = None
    """A single command with its arguments."""

    script: str | None = None  # FIXME mutually exclusive
    """Either a script starting with a #! line, or a shell command that is run with the current default shell."""

    creates: list[str] | None = None
    """Optional list of files this action may create."""

    def __call__(self, project_files: list[Path]) -> None:
        if self.cmd:
            execute(shlex.split(self.cmd), stdout=path_recorder(project_files))
        elif self.script is not None and self.script.strip().startswith("#!"):
            self._run_script(self.script, project_files)
        else:
            assert self.script is not None  # guaranteed by validation
            cmd = [os.environ.get("SHELL", "/bin/sh"), "-c", self.script]
            execute(cmd, stdout=path_recorder(project_files))

    def _run_script(self, script: str, project_files: list[Path]):
        with NamedTemporaryFile(
            "wt", prefix="getrel", delete_on_close=False
        ) as script_file:
            script_file.write(script.strip() + "\n")
            script_file.close()
            script_path = Path(script_file.name)
            script_path.chmod(0o700)
            execute([fspath(script_path)], stdout=path_recorder(project_files))

    def __str__(self) -> str:
        if self.cmd:
            return f"run the command `{self.cmd}`"
        elif self.script and "\n" in self.script:
            return f"run the following shell script:\n\n{indent(self.script, '      ')}"
        elif self.script:
            return f"run the shell command `{self.script}`"
        else:
            return "! Underconfigured action: " + super().__str__()


Action = UnpackAction | BinAction | LinkAction | ScriptAction

# FIXME: move everything below somewhere else (model?)


class Project(msgspec.Struct, omit_defaults=True, kw_only=True, dict=True):
    """A project configuration."""

    name: str
    install: list[Action] = []
    uninstall: list[Action] = []
    download: list[str] = []
    prerelease: bool = False

    @classmethod
    def load(cls, src: str | Path) -> Self:
        """
        Loads a project configuration from a YAML file.

        Args:
            src: The project name as a string to load from a configuration file in a standard location, or a file Path
        """
        if not isinstance(src, Path):
            config_file = xdg.BaseDirectory.load_first_config(
                "getrel", "projects", src + ".yaml"
            )
            src = Path(config_file) if config_file else Path(src)
        result = msgspec.yaml.decode(src.read_bytes(), type=cls)
        result.configured = datetime.fromtimestamp(src.stat().st_mtime)  # pyright: ignore[reportAttributeAccessIssue]
        return result

    @property
    def project_file(self) -> Path:
        return Path(
            xdg.BaseDirectory.load_first_config(
                "getrel", "projects", self.name + ".yaml"
            )
        )

    def save(self) -> bytes:
        """
        Save the configuration to a file in the default project location.

        Returns:
            serialized YAML representation as written to the file.
        """
        config_file = Path(
            xdg.BaseDirectory.save_config_path("getrel", "projects"),
            self.name + ".yaml",
        )
        serialized = msgspec.yaml.encode(self)
        config_file.write_bytes(serialized)
        return serialized

    def describe_actions(self):
        result = [
            "Download:",
            *(f"* {item}" for item in self.download),
        ]
        if self.install:
            result.append("\nInstall:")
            for action in self.install:
                result.append(f"* {action}")  # noqa: PERF401
        if self.uninstall:
            result.append("\nUninstall:")
            for action in self.uninstall:
                result.append(f"* {action}")  # noqa: PERF401
        return "\n".join(result)

    def do_install(self, state: ProjectState):
        """
        Run all configured install actions.
        """
        with WorkingDirectory(state.project_dir):
            for action in self.install:
                action(state.installed_files)

    def is_asset(self, file: str | Path) -> bool:
        """
        Returns True iff the given path points to an asset.

        An asset is a file directly downloaded from the project site, not created
        by an action.
        """
        file_ = str(file)
        return any(fnmatch(file_, pattern) for pattern in self.download)

    def do_uninstall(
        self, state: ProjectState, delete_assets=False, keep: Container[Path] = []
    ):
        """
        Uninstalls the given project.

        This first runs all configured uninstall actions, if any, and then removes all
        remaining files.

        Args:
            state: The current project state. Will be updated.
            delete_assets: If True, also delete downloaded assets.
            keep: Paths to files that should not be deleted.
        """
        with WorkingDirectory(state.project_dir):
            for action in self.uninstall:
                action(state.installed_files)
            remaining = []
            for file in reversed(state.installed_files):
                if (not delete_assets and self.is_asset(file)) or file in keep:
                    remaining.append(file)
                else:
                    try:
                        if file.is_dir():
                            file.rmdir()
                        else:
                            file.unlink()
                        logger.debug("Uninstalling %s: Removed %s", self.name, file)
                    except OSError as e:
                        level = (
                            logging.INFO
                            if isinstance(e, FileNotFoundError)
                            else logging.WARNING
                        )
                        logger.log(
                            level,
                            "Uninstalling %s: Could not delete %s (%s)",
                            self.name,
                            file,
                            e,
                        )
                        if file.exists():
                            remaining.append(file)
            state.installed_files.clear()
            state.installed_files.extend(remaining)
        state.installed = None


class Release(msgspec.Struct, omit_defaults=True):
    published: datetime
    version: str | None = None
    long_version: str | None = None
    description: str | None = None

    def __lt__(self, value: object, /) -> bool:
        if isinstance(value, Release):
            return self.published < value.published
        elif isinstance(value, datetime):
            return self.published < value
        else:
            return NotImplemented

    def __gt__(self, value: object, /) -> bool:
        if isinstance(value, Release):
            return self.published > value.published
        elif isinstance(value, datetime):
            return self.published > value
        else:
            return NotImplemented


class ProjectState(msgspec.Struct, omit_defaults=True):
    name: str
    description: str | None = None
    installed: Release | None = None
    available: Release | None = None
    installed_files: list[Path] = []
    configured: datetime | None = None

    def _is_external(self, file: Path) -> bool:
        """Returns true if the given path is outside the config dir"""
        return not (file.is_absolute() and file.is_relative_to(self.project_dir))

    def _is_binary(self, file: Path) -> bool:
        return file.is_file() and os.access(file, os.X_OK)

    def _absolute_file(self, file: Path) -> Path:
        return Path(DATA_DIR, self.name, file)

    def get_installed(self, binary=True, external=True, absolute=False):
        return [
            self._absolute_file(file) if absolute else file
            for file in self.installed_files or []
            if (not binary or self._is_binary(file))
            and (not external or self._is_external(file))
        ]

    @property
    def updateable(self):
        return self.available and (
            not self.installed or self.available.published > self.installed.published
        )

    @property
    def project_dir(self):
        return DATA_DIR / self.name

    def sanitize_files(self):
        self.installed_files = list(
            unique(
                f.relative_to(self.project_dir)
                if f.is_relative_to(self.project_dir)
                else f
                for f in self.installed_files
            )
        )

    def summarize_directory(
        self, project: Project, max_depth: int = 3, max_siblings: int = 15
    ):
        """
        Summarizes the contents of the project directory.

        It returns a rich.tree.Tree of files and directories. Both the tree depth and
        the number of siblings are shortened in case of large directories.
        For each file, there is an indicator of the FileType, and if it matches the
        source pattern of any config rule, that pattern is indicated as well.
        """
        project_dir = self.project_dir
        if not project_dir.exists():
            return Text(f"Project directory {project_dir} does not exist.")

        tree = Tree(f"[bold]{project.name}[/bold] ({project_dir})")

        settings = Settings.load()
        action_patterns = []
        for pattern in project.download:
            action_patterns.extend(settings.expand_arch(pattern))
        for action in project.install:
            action_patterns.extend(settings.expand_arch(action.source))
        action_patterns = list(unique(action_patterns))

        def add_to_tree(node: Tree, current_abs_path: Path, depth: int):
            if depth > max_depth:
                node.add("[dim]...[/dim]")
                return

            try:
                items = sorted(
                    current_abs_path.iterdir(), key=lambda p: (not p.is_dir(), p.name)
                )
            except OSError:
                return

            if len(items) > max_siblings:
                shown_items = items[:max_siblings]
                remaining = len(items) - max_siblings
            else:
                shown_items = items
                remaining = 0

            for item in shown_items:
                rel_path = item.relative_to(project_dir)

                label = Text(item.name)
                if item.is_dir():
                    label.stylize("bold blue")
                    branch = node.add(label)
                    add_to_tree(branch, item, depth + 1)
                else:
                    file_type = FileType(item)
                    label.append(f" ({file_type})", style="dim")

                    matches = [p for p in action_patterns if fnmatch(str(rel_path), p)]
                    if matches:
                        label.append(f" [match: {', '.join(matches)}]", style="green")

                    node.add(label)

            if remaining > 0:
                node.add(f"[dim]... ({remaining} more items)[/dim]")

        add_to_tree(tree, project_dir, 1)
        return tree


class GithubProject(Project, omit_defaults=True):
    kind: Literal["github"]  # pyright: ignore[reportGeneralTypeIssues]
    user: str  # pyright: ignore[reportGeneralTypeIssues]
    repo: str  # pyright: ignore[reportGeneralTypeIssues]

    @property
    def url(self):
        return f"https://github.com/{self.user}/{self.repo}"

    def __str__(self) -> str:
        return (
            f"GitHub project {self.user}/{self.repo}\n" + self.describe_actions() + "\n"
        )


class Settings(msgspec.Struct, omit_defaults=True):
    """
    General configuration for GetRel.
    """

    architectures: dict[str, list[str]] = {}
    """
    Architecture mapping for expanding {arch} in the source pattterns. Key is an architecture string, value is a list of possible alternatives.
    """

    add_rules: list[AddRule] = []
    """
    Heuristics to automatically generate actions when running 'getrel add'. Each rule can be matched to a file name or mime type, and if it
    matches, the given action is configured for the given source. 
    """

    def expand_arch(self, pattern: str) -> Iterable[str]:
        if "arch" not in field_names(pattern):
            return (pattern,)

        arch = machine()
        return unique(
            pattern.format(arch=replacement)
            for replacement in [arch, *self.architectures.get(arch, [])]
        )

    def expand(self, src: str | Path) -> Iterable[Path]:
        return [Path(expandvars(p)).expanduser() for p in self.expand_arch(fspath(src))]

    @classmethod
    @lru_cache(1)
    def load(cls) -> Self:
        config_file_ = xdg.BaseDirectory.load_first_config("getrel", "settings.yaml")
        if config_file_ and (config_file := Path(config_file_)).exists():
            return msgspec.yaml.decode(config_file.read_bytes(), type=cls)
        else:
            return cls()


class AddRule(msgspec.Struct, omit_defaults=True):
    """
    Make `getrel add` automatically configure actions for a given source.

    For each source and each rule:
    - if there are matches rules and the file name matches at least one of these as glob pattern AND
    - if there are mime rules and the detected mime type matches at least one of the mime glob patterns AND
    - the file name matches none of glob patterns under exclude
    then create the given action, replacing 'source' with the pattern for the given file.

    Use `mime: inode/directory` to match directories. Use 'then: skip' to explicitly not configure an action, even
    when one of the built-in rules match.
    """

    matches: list[str] = []
    exclude: list[str] = []
    mime: list[str] = []

    then: Action | Literal["skip"] = "skip"
    """
    The action to configure. If missing / null, explicitly do not configure an action
    even if a built-in heuristic would match.
    """


if __name__ == "__main__":
    if len(argv) < 2:  # noqa: PLR2004
        schema = msgspec.json.schema(Project)
        Path("getrel-project.schema.json").write_bytes(msgspec.json.encode(schema))
    elif len(argv) == 2:  # noqa: PLR2004
        struct = msgspec.toml.decode(
            Path(argv[1]).read_text(encoding="utf-8"), type=Project
        )
        print(struct)
        print(msgspec.json.encode(struct))


def write_schemas():
    path = Path(xdg.BaseDirectory.save_config_path("getrel"))
    path.joinpath("getrel-project.schema.json").write_bytes(
        msgspec.json.encode(msgspec.json.schema(GithubProject))
    )
    path.joinpath("getrel-settings.schema.json").write_bytes(
        msgspec.json.encode(msgspec.json.schema(Settings))
    )
    projects_path = Path(xdg.BaseDirectory.save_config_path("getrel", "projects"))
    projects_path.joinpath("getrel-project.schema.json").write_bytes(
        msgspec.json.encode(msgspec.json.schema(GithubProject))
    )
