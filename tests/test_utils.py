from unittest.mock import patch, MagicMock

import pytest
import requests

from getrel.utils import first, FileType, fetch_if_newer


def test_first():
    assert first([1, 2, 3]) == 1
    assert first([1], strict=True) == 1
    with pytest.raises(ValueError):
        first([1, 2, 3], strict=True)


def test_first_default():
    assert first([], default=1) == 1
    with pytest.raises((ValueError)):
        first([])


def test_filetype():
    ft = FileType(__file__)
    assert ft.mime == 'text/x-script.python'
    assert "Python" in ft.description
    assert ft.executable
    assert not ft.archive
    assert ft.file.samefile(__file__)


def test_fetch_if_newer():
    cache = {'ETag': 'test'}
    not_modified = MagicMock()
    not_modified.status_code = requests.codes.not_modified
    with patch('requests.get', MagicMock(return_value=not_modified)) as get:
        url = 'https://github.com/foo'
        result = fetch_if_newer(url, cache)
        assert result == False
        assert 'last-request' in cache
        get.assert_called_once_with(url, headers={'If-None-Match': cache['ETag']})