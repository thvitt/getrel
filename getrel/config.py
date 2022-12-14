import typing
from abc import ABC, abstractmethod
from collections.abc import Mapping, MutableMapping
from datetime import timedelta
from difflib import unified_diff
from operator import getitem
from pathlib import Path
from typing import Optional, TypeVar, Union
from contextlib import contextmanager
from functools import lru_cache
from unittest.mock import MagicMock

from durations import Duration
from tomlkit.toml_document import TOMLDocument
import json
import os

import xdg
import tomlkit

import logging
logger = logging.getLogger(__name__)

APP_NAME = 'getrel'

"""
- ~/.config/{APP_NAME}/settings.toml  contains the optional configuration file
- ~/.config/{APP_NAME}/projects.toml  has 
- ~/.local/{APP_NAME}/{project} is the project specific 
"""

console = None

class BaseSettings(ABC, MutableMapping):
    """
    Thin wrapper around a structured document that keeps track of the file to load and save to.

    ``settings = Settings('foo.toml')`` create a new settings object and load the data from foo.toml,
    if that file exists, otherwise an empty TOML document is created. The data can be accessed using
    settings.data or directly (e.g., 'settings["key"]`). ``settings.save()`` will save the data
    back to foo.toml, creating it and its parent directories if needed. 

    When used as a context manager, the settings file will be saved automatically upon
    successfully leaving the `with` block.

    This is the abstract base class. For support for a specific file format, subclass 
    and implement the three static methods dumps() to serialize data, loads() to de serialize
    data and new_data() to create a new settings record.

    Attributes:
        store: the file used to store the data. Not guaranteed to exist (e.g., for a new document')
        data: toml document with the data
    
    """
    store: Path
    data: MutableMapping
    last_state: Optional[str]

    @staticmethod
    @abstractmethod
    def dumps(data: MutableMapping) -> str:
        ...

    @staticmethod
    @abstractmethod
    def loads(content: str) -> MutableMapping:
        ...

    @staticmethod
    @abstractmethod
    def new_data() -> MutableMapping:
        ...

    def load(self, file: Optional[Path] = None):
        if file is None:
            file = self.store
        with file.open('rt', encoding='utf-8') as f:
            self.data = self.loads(f.read())
        self.last_state = self.dumps(self.data)
        return self.data

    def save(self, file: Optional[Path] = None, force: bool = False):
        """
        Serializes the data and stores it to the given file, or to self.store. 
        Parent directories are created as needed, the file is overwritten if it
        exists.
        """
        new_content = self.dumps(self.data)
        if force or new_content != self.last_state:
            assert isinstance(self.loads(new_content), Mapping)
            if file is None:
                file = self.store
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(new_content)
            if logger.isEnabledFor(logging.DEBUG):
                if self.last_state:
                    logger.debug('Saved %s, diff: %s', self.store, '\n'.join(unified_diff(self.last_state.split('\n'), new_content.split('\n'))))
                else:
                    logger.debug('Created %s, content: %s', self.store, new_content)
            self.last_state = new_content

    def __init__(self, file: Union[Path, str], data: Optional[Mapping] = None, save_on_error=True) -> None:
        """
        Create, load and initialize a new settings instance.

        The given file can later be accessed using self.store. If it exists, the data is
        loaded from it, otherwise it is initialized to an empty document.

        If the optional data is given, the settings object will be updated to it;
        if both file and data exist, the file is read and the resulting mapping
        updated using the data from the argument.
        """
        self.last_state = None
        self.save_on_error = save_on_error
        if isinstance(file, str):
            file = Path(file)
        self.store = file
        try:
            self.load()
        except Exception as e:
            self.data = self.new_data()
            logger.debug('Could not load %s: %s. Using new data.', file, e, exc_info=True)

        if data is not None:
            self.data.update(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type is None or self.save_on_error:
            self.save()

    def __getattr__(self, name):
        return getattr(self.data, name)

    def __getitem__(self, item):
        return self.data[item]

    def __setitem__(self, item, value):
        self.data[item] = value

    def __delitem__(self, item):
        del self.data[item]

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return f'<{self.__class__.__name__}({str(self.store)!r}, {self.data!r})>'


class Settings(BaseSettings):
    data: TOMLDocument

    @staticmethod
    def dumps(data) -> str:
        return tomlkit.dumps(data)

    @staticmethod
    def loads(str):
        return tomlkit.loads(str)

    @staticmethod
    def new_data():
        return tomlkit.document()


class JSONSettings(BaseSettings):

    @staticmethod
    def dumps(data: MutableMapping) -> str:
        return json.dumps(data, indent=2)

    @staticmethod
    def loads(data):
        return json.loads(data)

    @staticmethod
    def new_data() -> MutableMapping:
        return dict()


@lru_cache()
def edit_projects() -> Settings:
    return Settings(xdg.xdg_config_home() / APP_NAME / 'projects.toml')


def project_directory(project_name: Optional[str] = None) -> Path:
    root = xdg.xdg_data_home() / APP_NAME
    if project_name:
        return root / project_name
    else:
        return root


def project_state_directory(project_name: str, create=False) -> Path:
    path = project_directory(project_name) / ('.' + APP_NAME)
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    return path


def verb_or_spec(value: Union[str, Mapping, None], allowed_verbs=None):
    if isinstance(value, Mapping):
        items = value.items()
        if len(items) == 0:
            verb, arg = None, None
        elif len(items) == 1:
            verb, arg = next(iter(items))
        else:
            raise TypeError("More than one key-value pair is not allowed here")
    else:
        verb, arg = value, None
    if allowed_verbs and verb not in allowed_verbs:
        raise ValueError(f"verb must be one of {allowed_verbs}")
    return verb, arg


T = TypeVar('T')
class SettingAttribute:

    _no_default = object()

    def __init__(self, name: str,
                 default: T =_no_default,
                 *,
                 autosave: bool = False,
                 dtype: Optional[typing.Type[T]] = None,
                 parse: Optional[typing.Callable[[typing.Any], T]] = None,
                 unparse: Optional[typing.Callable[[T], typing.Any]] = None):
        """
        Descriptor for saving stuff to a 'BaseSettings' instance.

        Args:
            name: Name of the value
            default: Optional default value (if value is not present in settings object).
            autosave: call save() on the settings object after modifying the attribute
            dtype: return datatype. If present and parse is missing, return value will be converted to this type.
            parse: function to convert between BaseSettings??? representation and return value.
            unparse: other way around.
        """
        self.dtype = dtype
        self.name = name
        self.default = default
        self.autosave = autosave

        if parse is not None:
            self.parse = parse
        elif dtype is not None:
            self.parse = dtype
        else:
            self.parse = lambda x: x

        if unparse is not None:
            self.unparse = unparse
        else:
            self.unparse = lambda x: x

    def __get__(self, obj: BaseSettings, objtype=None) -> T:
        try:
            return self.parse(obj[self.name])
        except KeyError:
            if self.default is self._no_default:
                raise
            else:
                return self.default

    def __set__(self, obj: BaseSettings, value: T):
        obj[self.name] = self.unparse(value)
        if self.autosave:
            obj.save()

def _parse_duration(s: str) -> timedelta:
    return timedelta(seconds=Duration(str(s)).to_seconds())

def _unparse_duration(d: timedelta) -> str:
    return str(int(d.total_seconds()))+'s'

class _ProgramSettings(Settings):
    fetch_delay = SettingAttribute('fetch_delay', default=timedelta(days=1), dtype=timedelta, parse=_parse_duration, unparse=_unparse_duration)
    update_delay = SettingAttribute('update_delay', default=timedelta(days=1), dtype=timedelta, parse=_parse_duration, unparse=_unparse_duration)

settings = _ProgramSettings(xdg.xdg_config_home() / APP_NAME / 'settings.toml')

def expand_path(path: Union[str, os.PathLike], project_name=None, **kwargs) -> Path:
    if project_name:
        kwargs['PROJECT'] = project_name
        kwargs['PROJECT_DIR'] = project_directory(project_name)
    with update_environ(kwargs):
        return Path(os.path.expandvars(os.path.expanduser(path)))


@contextmanager
def update_environ(extra_env):
    backup = dict(os.environ)
    os.environ.update({str(k): str(v) for k, v in extra_env.items()})
    yield os.environ
    os.environ.update(backup)
    for key in set(os.environ) - set(backup):
        del os.environ[key]


@lru_cache()
def edit_project_state(project_name: str) -> BaseSettings:
    return JSONSettings(project_state_directory(project_name) / 'state.json')

def get_progress(**kwargs):
    try:
        from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
        if console and not 'console' in kwargs:
            kwargs['console'] = console

        return Progress(
                TextColumn("[bold blue]{task.description}", justify="right"),
                BarColumn(bar_width=None),
                "[progress.percentage]{task.percentage:>3.1f}%",
                "???",
                DownloadColumn(),
                "???",
                TransferSpeedColumn(),
                "???",
                TimeRemainingColumn(),
                **kwargs
        )
    except ImportError:
        from unittest.mock import Mock
        return MagicMock()
