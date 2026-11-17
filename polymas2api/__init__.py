"""polymas2api — the 智慧树 (Polymas) AI assistant as an OpenAI-compatible API."""

from __future__ import annotations

from . import constants as C
from .adapter import OpenAIAdapter
from .config import Config, ConfigError, load_config
from .server import Gateway, serve
from .upstream import PolymasClient, UpstreamError, parse_sse

__version__ = "0.1.0"

__all__ = [
    "C", "Config", "ConfigError", "load_config", "Gateway", "serve",
    "PolymasClient", "UpstreamError", "parse_sse", "OpenAIAdapter",
    "__version__",
]
