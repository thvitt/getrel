from pathlib import Path
from tarfile import is_tarfile
from typing import TypeVar, Iterable, MutableMapping
import stat
from mimetypes import guess_type
from typing import Optional
from zipfile import is_zipfile

import requests
import logging
logger = logging.getLogger(__name__)


try:
    from humanize import naturalsize
except ImportError:
    def naturalsize(size: int, **kwargs):
        return str(size)


T = TypeVar('T')
_no_default = object()

def first(iterable: Iterable[T], *, default=_no_default, strict=False) -> T:
    """
    Returns the first element of the given iterable.

    Args:
        iterable: an iterable
        default: if present and the iterable is empty, return default instead.
        strict: if true, exactly one item is allowed in the iterable.
    Raises:
        ValueError if iterable is empty and default is missing or if strict is true and iterable contains not exactly one item
    """
    try:
        iterator = iter(iterable)
        result = next(iterator)
    except StopIteration:
        if strict or default is _no_default:
            raise ValueError(f'{iterable} is empty')
        else:
            return default # type: ignore
    if strict:
        try:
            second = next(iterator)
        except StopIteration:
            return result
        raise ValueError(f'More than one value: {[result, second, ...]}')
    else:
        return result


try:
    import magic
except ImportError:
    magic = None 


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
    mime: Optional[str]
    description: str
    executable: bool = False 
    archive: bool = False

    def __init__(self, file: Path):
        if not isinstance(file, Path):
            file = Path(file)
        self.file = file
        if magic is not None:
            self.mime = magic.from_file(file, mime=True)
            self.description = magic.from_file(file) or 'unknown'
        else:
            self.mime = guess_type(file, strict=False)[0]
            self.description = self.mime or "unknown"

        if file.is_file() and file.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            self.executable = True
        elif self.mime is not None and ('executable' in self.mime or 'script' in self.mime):
            self.executable = True
        elif is_tarfile(file) or is_zipfile(file):
            self.archive = True
        else:
            with file.open(errors='ignore') as f:
                if f.read(2) == '#!':
                    self.executable = True

    def __str__(self):
        result = self.mime
        app = []
        if self.executable:
            app.append('executable')
        if self.archive:
            app.append('archive')
        if app:
            result += f" ({' '.join(app)})"
        return result



def fetch_if_newer(url: str, cache: MutableMapping, *, download_file: Path | None = None, json: bool | str = False,
                   return_response: bool = False, cache_headers: bool = False, headers=None, **kwargs):
    """
    Retrieves the given URL unless it has not been modified.

    The function creates a HTTP request to download the given URL unless it has
    not been modified since the last access. The last access is managed via the
    given cache.

    Args:
        url: The URL to get
        cache: A dictionary to use for caching, may be empty. The function will use the keys 'ETag' and 'Last-Modified' for the corresponding response headers and
               the 'data' key for the response unless download_file is given.
        download_file:
            if given, the request's result will be saved to this file instead of to the 'data' key of the result dict.
        json:
            if True, explicitely request JSON. cache['ðata'] will be assigned the parsed JSON result. If a str, set the Accept: header
            to the string and handle it as if it were True otherwise.
        return_response:
            return the response object if a full 200 response has been retrieved.
    Returns:
        True if actual data has been retrieved, updating the cache dict and optionally writing to the download_file as side effect.
        False if the data has not been newer.
        a response if return_response is true and True would have been returned.
    """

    if headers is None:
        headers = {}
    if json:
        headers['Accept'] = 'application/json'
    if 'ETag' in cache:
        headers['If-None-Match'] = str(cache['ETag'])
    if 'Last-Modified' in cache:
        headers['If-Modified-Since'] = str(cache['Last-Modified'])
    response = requests.get(url, headers=headers, **kwargs)
    if response.status_code == requests.codes.not_modified:
        logger.debug('%s: Not modified', url)
        return False
    response.raise_for_status()
    # if we land here, a full (updated or new) response has been received.
    if cache_headers:
        cache['url'] = response.url
    if 'ETag' in response.headers:
        cache['ETag'] = response.headers['ETag']
    if 'Last-Modified' in response.headers:
        cache['Last-Modified'] = response.headers['Last-Modified']
    if cache_headers:
        cache['headers'] = dict(response.headers)
    if download_file:
        logger.debug('%s: Downloading to %s', url, download_file)
        download_file.parent.mkdir(parents=True, exist_ok=True)
        with download_file.open('wb') as f:
            for chunk in response.iter_content(chunk_size=None):
                f.write(chunk)
    elif json:
        logger.debug('%s: Downloading JSON to cache', url)
        cache['data'] = response.json()
    else:
        logger.debug('%s: Downloading data to cache', url)
        try:
            json = response.json()
            cache['data'] = response.json()
        except requests.JSONDecodeError:
            if response.encoding is not None:
                cache['data'] = response.text
            else:
                cache['data'] = response.content
    if return_response:
        return response
    else:
        return True
