from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Union
import datetime
import functools
import hashlib
import json
import logging
import os
import pickle
import shutil


# TODO: use typing.Mapping with stricter structure
_EntryDict = Dict
_IndexDict = Dict[str, _EntryDict]


_CACHE_DIR = '.derpcache'
_CACHE_INDEX_FILE = 'index.json'


logger = logging.getLogger(__name__)


def _get_root_dir() -> str:
    return os.environ.get('DERPCACHE_ROOT_DIR', '.')


def _get_cache_path(s: str = '') -> str:
    return os.path.join(_get_root_dir(), _CACHE_DIR, s)


def _get_index_path() -> str:
    return _get_cache_path(_CACHE_INDEX_FILE)


def _is_non_str_iterable(x: Any) -> bool:
    return hasattr(x, '__iter__') and not isinstance(x, str)


def _sort_nested_dicts(value: Union[dict, list, Any]) -> Union[dict, list, Any]:
    """Sort nested dicts by keys so casting it will produce a deterministic string.

    Warning: Thar be edge cases."""

    if isinstance(value, dict):
        value = {k: _sort_nested_dicts(v) for k, v in sorted(value.items(), key=str)}
    elif _is_non_str_iterable(value):
        value = [_sort_nested_dicts(x) for x in value]
    return value


def _describe_callable(f: Callable) -> str:
    """Note: Some callables are missing a :attr:`__qualname__`, so including `type()`
    provides at least some information."""

    mod = f.__module__
    name = getattr(f, '__qualname__', str(type(f)))
    return f'{mod}.{name}'


def _to_string(arg: Any) -> str:
    arg = _sort_nested_dicts(arg)
    return str(arg)


def _hash_args(*args, **kwargs) -> str:
    args_str = str(sorted(_to_string(x) for x in args))
    kwargs_str = _to_string(kwargs)
    string = args_str + kwargs_str
    return hashlib.sha256(string.encode()).hexdigest()[:8]


def _read_index() -> _IndexDict:
    with open(_get_index_path(), 'r') as f:
        index = json.load(f)
    return index


def _write_index(index: _IndexDict) -> None:
    with open(_get_index_path(), 'w') as f:
        json.dump(index, f)


def _add_index_entry(index: _IndexDict, hash: str, entry: _EntryDict) -> None:
    index[hash] = entry
    _write_index(index)


def get_by_hash(hash: str) -> Any:
    with open(_get_cache_path(hash), 'rb') as f:
        value = pickle.load(f)
    return value


def _write_object_by_hash(hash: str, value: Any) -> None:
    with open(_get_cache_path(hash), 'wb') as f:
        pickle.dump(value, f)


def _remove_objects(to_remove: List[str]) -> None:
    for hash in to_remove:
        os.remove(_get_cache_path(hash))


def _remove_entries(index: _IndexDict, to_remove: List[str]) -> _IndexDict:
    index = {k: v for k, v in index.items() if k not in to_remove}
    _write_index(index)
    return index


def _is_expired(entry: _EntryDict) -> bool:
    expires_after = entry.get('expires_after')
    if expires_after:
        called_at = datetime.datetime.fromisoformat(entry['called_at'])
        expires_after = datetime.timedelta(seconds=expires_after)
        now = datetime.datetime.utcnow()
        expired = called_at + expires_after >= now
    else:
        expired = False
    return expired


def _remove_expired_items(index: _IndexDict) -> _IndexDict:
    to_remove = []
    for hash, entry in index.items():
        if _is_expired(entry):
            to_remove.append(hash)
    index = _remove_entries(index, to_remove)
    _remove_objects(to_remove)
    return index


def _sort_index(index: _IndexDict) -> _IndexDict:
    index = {k: v for k, v in sorted(index.items(), key=lambda x: x[1]['called_at'])}
    return index


def get_index(clear_expired: bool = True) -> _IndexDict:
    index = _read_index()
    if clear_expired:
        index = _remove_expired_items(index)
    index = _sort_index(index)
    return index


def _init_cache() -> None:
    if _CACHE_DIR not in os.listdir(_get_root_dir()):
        os.mkdir(_CACHE_DIR)
        _write_index({})


def clear_cache() -> None:
    try:
        shutil.rmtree(_get_cache_path())
    except FileNotFoundError:
        pass


def _expires_after_to_float(expires_after: Union[float, datetime.timedelta]) -> float:
    if isinstance(expires_after, datetime.timedelta):
        expires_after = expires_after.total_seconds()
    return expires_after


def _format_entry(
    f: Callable,
    called_at: str,
    expires_after: Optional[Union[float, datetime.timedelta]],
    annotation: Optional[str],
    hash_annotation: bool,
) -> _EntryDict:
    entry = {
        'callable': _describe_callable(f),
        'called_at': called_at,
    }
    if expires_after:
        # mypy thinks `expires_after` is a string here
        entry['expires_after'] = _expires_after_to_float(expires_after)  # type: ignore
    if annotation:
        entry['annotation'] = annotation
        # mypy thinks `hash_annotation` is a string here
        entry['hash_annotation'] = hash_annotation  # type: ignore
    return entry


def cache(
    f: Callable,
    *args,
    _expires_after: Optional[Union[float, datetime.timedelta]] = None,
    _annotation: Optional[str] = None,
    _hash_annotation: bool = False,
    **kwargs,
) -> Any:
    _init_cache()
    hash = _hash_args(
        # lazy, but keeps :meth:`_hash_args` dumb
        _describe_callable(f),
        _expires_after,
        _annotation if _hash_annotation else '',
        *args,
        **kwargs,
    )
    index = get_index(clear_expired=True)
    if hash in index:
        value = get_by_hash(hash)
        logger.debug('cache hit')
    else:
        logger.debug('caching...')
        called_at = datetime.datetime.utcnow().isoformat()
        value = f(*args, **kwargs)
        _write_object_by_hash(hash, value)
        _add_index_entry(
            index,
            hash,
            entry=_format_entry(
                f,
                called_at,
                _expires_after,
                _annotation,
                _hash_annotation,
            ),
        )
        logger.debug('caching successful.')
    return value


def cache_wrapper(
    _expires_after: Optional[Union[float, datetime.timedelta]] = None,
    _annotation: Optional[str] = None,
    _hash_annotation: bool = False,
) -> Callable:
    """TODO: support wrapping bound methods."""

    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapped(*args, **kwargs) -> Any:
            return cache(
                f,
                *args,
                **kwargs,
                _expires_after=_expires_after,
                _annotation=_annotation,
                _hash_annotation=_hash_annotation,
            )

        return wrapped

    return decorator
