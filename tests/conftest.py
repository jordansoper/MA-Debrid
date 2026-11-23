"""Standalone test bootstrap.

Before pytest collects the tests it may import the provider folder's ``__init__.py`` directly
(the folder name contains a space, so pytest cannot resolve it as a normal package). This
bootstrap therefore:

* imports the folder as the ``ma_debrid`` package, exactly as Music Assistant imports a
  community provider by its manifest domain;
* provides the handful of Music Assistant server symbols that entry point imports when the
  server is not installed (the server is Linux-only, so the suite must run without it);
* reuses that package for the fallback ``__init__`` name pytest imports the folder under.

Only the provider's own modules are exercised by the tests; the stub exists so the entry point
can be imported, not to emulate the server.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _install_server_stubs() -> None:
    """Provide the import surface the provider entry point needs."""
    for name in list(sys.modules):
        if name == "music_assistant" or name.startswith("music_assistant."):
            del sys.modules[name]

    def module(name: str) -> types.ModuleType:
        created = types.ModuleType(name)
        created.__path__ = []
        sys.modules[name] = created
        return created

    def use_cache(*args: Any, **kwargs: Any) -> Any:
        """Stand in for Music Assistant's cache decorator."""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def decorator(func: Any) -> Any:
            return func

        return decorator

    class Throttler:
        """Stand in for Music Assistant's async rate limiter."""

        def __init__(self, rate_limit: int, period: float = 1.0) -> None:
            self.rate_limit = rate_limit
            self.period = period

        async def __aenter__(self) -> float:
            return 0.0

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class MusicProvider:
        """Stand in for Music Assistant's music provider base class."""

        def __init__(
            self,
            mass: Any,
            manifest: Any,
            config: Any,
            supported_features: set[Any] | None = None,
        ) -> None:
            self.mass = mass
            self.manifest = manifest
            self.config = config
            self.supported_features = supported_features or set()
            self.instance_id = getattr(config, "instance_id", "ma_debrid")
            self.domain = getattr(manifest, "domain", "ma_debrid")
            self.name = getattr(manifest, "name", "MA Debrid")
            self.logger = logging.getLogger("ma_debrid")

    module("music_assistant")
    module("music_assistant.controllers")
    cache = module("music_assistant.controllers.cache")
    cache.use_cache = use_cache
    module("music_assistant.helpers")
    throttle = module("music_assistant.helpers.throttle_retry")
    throttle.Throttler = Throttler
    module("music_assistant.models")
    provider_base = module("music_assistant.models.music_provider")
    provider_base.MusicProvider = MusicProvider


def _ensure_server_imports() -> None:
    """Use the installed server when it imports on this platform, else install the stub."""
    try:
        from music_assistant.controllers.cache import use_cache  # noqa: F401
        from music_assistant.helpers.throttle_retry import Throttler  # noqa: F401
        from music_assistant.models.music_provider import MusicProvider  # noqa: F401
    except ImportError:
        _install_server_stubs()


def _import_provider_package() -> types.ModuleType:
    """Import the provider folder as the ``ma_debrid`` package."""
    spec = importlib.util.spec_from_file_location(
        "ma_debrid",
        PROJECT_ROOT / "__init__.py",
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load the MA Debrid provider package")
    package = importlib.util.module_from_spec(spec)
    sys.modules["ma_debrid"] = package
    spec.loader.exec_module(package)
    return package


_ensure_server_imports()
_provider_package = _import_provider_package()
# pytest's fallback name for the provider folder's __init__.py when collecting from the root
sys.modules["__init__"] = _provider_package
