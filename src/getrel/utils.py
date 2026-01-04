from collections.abc import Callable, Iterable, Mapping
from os import chdir
from os.path import expandvars
from pathlib import Path
from typing import Any


def expand(src: str | Path) -> Path:
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


def dec_hook(type_: type, obj: Any) -> Any:  # noqa: A002
    if type_ is Path:
        return Path(obj)
    else:
        raise NotImplementedError(f"Objects of type {type_} are not supported.")
