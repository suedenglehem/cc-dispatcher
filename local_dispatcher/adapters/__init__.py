from .base import StreamInfo, WorkerAdapter, WorkerLaunch  # noqa: F401
from .claude import ClaudeAdapter  # noqa: F401
from .cline import ClineAdapter  # noqa: F401

_REGISTRY = {
    "claude": ClaudeAdapter,
    "cline": ClineAdapter,
}


def get_adapter(name: str) -> WorkerAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown adapter {name!r}") from None
