import stat
from collections.abc import Callable, Iterable, Mapping
from os import chdir
from pathlib import Path
from string import Formatter
from tarfile import is_tarfile
from typing import Any
from zipfile import is_zipfile

import magic


def field_names(pattern: str) -> set[str]:
    """
    Extracts all field names in the given format string.
    """
    return {
        field
        for (_literal, field, _format, _conversion) in Formatter().parse(pattern)
        if field is not None
    }


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

    def __str__(self):
        return str(self.directory)


def split_list[T](
    source: Iterable[T],
    predicate: Callable[[T], bool] = bool,
) -> tuple[list[T], list[T]]:
    accepted = []
    rejected = []
    for item in source:
        if predicate(item):
            accepted.append(item)
        else:
            rejected.append(item)
    return accepted, rejected


def split_dict[K, V](
    source: Mapping[K, V], predicate: Callable[[V], bool] = bool
) -> tuple[dict[K, V], dict[K, V]]:
    accepted = {}
    rejected = {}
    for key, value in source.items():
        if predicate(value):
            accepted[key] = value
        else:
            rejected[key] = value
    return accepted, rejected


## Encode / Decode stuff with our data types


def enc_hook(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    else:
        raise NotImplementedError(f"Objects of type {type(obj)} are not supported.")


def dec_hook(type_: type, obj: Any) -> Any:
    if type_ is Path:
        return Path(obj)
    else:
        raise NotImplementedError(f"Objects of type {type_} are not supported.")


def first[T](iterable: Iterable[T], /) -> T:
    try:
        return next(iter(iterable))
    except StopIteration as e:
        raise IndexError(f"{iterable} is empty") from e


def unique[T](iterable: Iterable[T], /) -> Iterable[T]:
    seen = set()
    for item in iterable:
        if item not in seen:
            yield item
            seen.add(item)


def double_braces(src: str) -> str:
    return src.replace("{", "{{").replace("}", "}}")


class FileType:
    """
    Tries to detect the filetype of the given file (which may be a string or
    path). It will use libmagic if available.

    Properties:
        file: Path of the file
        mime: detected MIME type (or None, if it could not be detected)
        description: textual form of the type
        executable: if True, we guess it’s some kind of executable file
        archive: if True, its an archive we can unpack
    """

    file: Path
    mime: str | None
    description: str
    executable: bool = False
    archive: bool = False

    def __init__(self, file: Path | str):
        if not isinstance(file, Path):
            file = Path(file)
        self.file = file
        if file.is_dir():
            self.mime = "inode/directory"
            self.description = "Directory"
            return
        else:
            self.mime = magic.from_file(file, mime=True)
            self.description = magic.from_file(file) or "unknown"

        if (
            file.is_file()
            and file.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ) or (
            self.mime is not None
            and ("executable" in self.mime or "script" in self.mime)
        ):
            self.executable = True
        elif is_tarfile(file) or is_zipfile(file) or str(file).endswith(".zst"):
            self.archive = True
        elif file.is_file():
            with file.open(errors="ignore") as f:
                if f.read(2) == "#!":
                    self.executable = True

    def __str__(self):
        result = self.mime or ""
        app = []
        if self.executable:
            app.append("executable")
        if self.archive:
            app.append("archive")
        if app:
            result += f" ({' '.join(app)})"
        return result
