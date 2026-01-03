import logging
from pathlib import Path

import msgspec
import xdg.BaseDirectory
from msgspec.json import decode

from getrel.actions import GithubProject, ProjectState
from getrel.utils import dec_hook, enc_hook

logger = logging.getLogger()


def load_project_configs():
    config_dir = Path(xdg.BaseDirectory.load_first_config("getrel", "projects"))
    for config_file in config_dir.glob("*.yaml"):
        yield GithubProject.load(config_file)


def first_config_path(*resource: str | Path) -> Path | None:
    cand = xdg.BaseDirectory.load_first_config(*resource)
    if cand:
        return Path(cand)
    else:
        return None


def load_project_states():
    state_file = Path(
        xdg.BaseDirectory.save_state_path(
            "getrel",
        ),
        "projects.msgpack",
    )
    return {
        state.name: state
        for state in msgspec.msgpack.decode(
            state_file.read_bytes(), type=list[ProjectState], dec_hook=dec_hook
        )
    }


def get_state_path():
    return Path(xdg.BaseDirectory.save_state_path("getrel")) / "projects.msgpack"


def save_state(states: dict[str, ProjectState]):
    state_file = get_state_path()
    state_file.write_bytes(
        msgspec.msgpack.encode(list(states.values()), enc_hook=enc_hook)
    )
