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
        serialize=True,
        rotation="weekly",
        compression="gz",
    )


def is_debug() -> bool:
    """Whether getrel's own logger is currently configured at debug level."""
    return _state.debug
