import json
import logging
from dataclasses import dataclass

from loguru import logger
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback

from getrel.config import get_state_path


@dataclass
class _State:
    debug: bool = False


_state = _State()


def _json_format(record) -> str:
    """
    Format function for the JSON log sink: serializes just the `record` dict
    (as loguru's own `serialize=True` would put it under a "record" key),
    without the redundant pre-rendered "text" field.

    Dynamic format functions must return a fixed template, not pre-rendered
    content: loguru parses the returned string for "<tag>" markup, which
    breaks on arbitrary data (e.g. a message containing "<foo>"). So the
    actual JSON is stashed in record["extra"] and only referenced by field
    name here; the substitution happens after markup parsing.
    """
    exception = record["exception"]
    if exception is not None:
        exception = {
            "type": None if exception.type is None else exception.type.__name__,
            "value": exception.value,
            "traceback": bool(exception.traceback),
        }
    entry = {
        "time": record["time"].isoformat(),
        "level": {"name": record["level"].name, "no": record["level"].no},
        "message": record["message"],
        "name": record["name"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
        "process": {"id": record["process"].id, "name": record["process"].name},
        "thread": {"id": record["thread"].id, "name": record["thread"].name},
        "exception": exception,
        "extra": dict(record["extra"]),
    }
    record["extra"]["json_line"] = json.dumps(entry, default=str, ensure_ascii=False)
    return "{extra[json_line]}\n"


def setup_logging(verbose: int, console: Console) -> None:
    """
    Configures loguru to log through rich, with -v/--verbose controlling the
    verbosity of getrel's own messages (one level up) and everything else.
    """
    global_level = logging.WARNING - (verbose // 2) * 10
    local_level = logging.WARNING - ((verbose + 1) // 2 * 10)
    _state.debug = local_level <= logging.DEBUG

    logger.remove()
    logger.add(
        RichHandler(console=console, rich_tracebacks=_state.debug),
        format=lambda _: "{message}",
        filter={"": global_level, "getrel": local_level},
    )
    logger.add(
        get_state_path().parent.joinpath("log.jsonl"),
        format=_json_format,
        rotation="weekly",
        compression="gz",
    )


def is_debug() -> bool:
    """Whether getrel's own logger is currently configured at debug level."""
    return _state.debug
