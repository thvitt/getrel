import logging
from dataclasses import dataclass

from loguru import logger
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback


@dataclass
class _State:
    debug: bool = False


_state = _State()


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
        # a plain string format would have "\n{exception}" auto-appended, duplicating
        # the traceback that RichHandler itself renders from the record's exc_info
        format=lambda _record: "{message}\n",
        filter={"": global_level, "getrel": local_level},
    )
    if _state.debug:
        install_rich_traceback(console=console, suppress=["cyclopts"])


def is_debug() -> bool:
    """Whether getrel's own logger is currently configured at debug level."""
    return _state.debug
