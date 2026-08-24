import inspect
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


class InterceptHandler(logging.Handler):
    """
    Route standard logging to loguru, as well.

    Source: https://loguru.readthedocs.io/en/stable/overview.html#entirely-compatible-with-standard-logging
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def json_formatter(record):
    serialized = json.dumps(record, default=format)
    record["extra"]["serialized"] = serialized
    return "{extra[serialized]}\n"


def setup_logging(verbose: int, console: Console) -> None:
    """
    Configures loguru to log through rich, with -v/--verbose controlling the
    verbosity of getrel's own messages (one level up) and everything else.
    """
    global_level = logging.WARNING - (verbose // 2) * 10
    local_level = logging.WARNING - ((verbose + 1) // 2 * 10)
    _state.debug = local_level <= logging.DEBUG
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    logger.remove()
    logger.add(
        lambda msg: console.print(
            msg, end="", style="bold" if msg.record["level"].no >= logging.ERROR else ""
        ),
        format="{level.icon} {message} [dim]([link=file.path]{name}[/link]:{function}:{line})[/dim]",
        filter={"": global_level, "getrel": local_level},
    )
    logger.add(
        get_state_path().parent.joinpath("log.jsonl"),
        format=json_formatter,
        rotation="weekly",
        compression="gz",
    )


def is_debug() -> bool:
    """Whether getrel's own logger is currently configured at debug level."""
    return _state.debug
