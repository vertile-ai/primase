"""Pluggable backend registry.

Built-in adapters (Harbor, Pier) are always importable. They are also advertised
via the ``primase.backends`` entry-point group so a third-party package can
register ``pip install``-able backends without patching primase::

    [project.entry-points."primase.backends"]
    mybench = "mypkg.backend:MyBackend"
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from .base import Backend, RunContext, Validation

_REGISTRY: dict[str, type[Backend]] | None = None


def _builtins() -> dict[str, type[Backend]]:
    from .harbor import HarborBackend
    from .pier import PierBackend

    return {"harbor": HarborBackend, "pier": PierBackend}


def _entry_point_backends() -> dict[str, type[Backend]]:
    found: dict[str, type[Backend]] = {}
    try:
        eps = entry_points(group="primase.backends")
    except Exception:  # noqa: BLE001
        return found
    for ep in eps:
        try:
            found[ep.name] = ep.load()
        except Exception:  # noqa: BLE001
            continue
    return found


def get_registry() -> dict[str, type[Backend]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = {**_builtins(), **_entry_point_backends()}
    return _REGISTRY


def register_backend(name: str, cls: type[Backend]) -> None:
    """Programmatic registration (tests, in-process plugins)."""
    get_registry()[name] = cls


def reset_registry() -> None:
    """Drop the cached registry (used by tests)."""
    global _REGISTRY
    _REGISTRY = None


def list_backends() -> list[str]:
    return sorted(get_registry())


def get_backend(name: str, root: Path) -> Backend:
    try:
        return get_registry()[name](root)
    except KeyError:
        raise KeyError(f"unknown backend {name!r} (known: {', '.join(list_backends())})") from None


__all__ = [
    "Backend",
    "RunContext",
    "Validation",
    "get_backend",
    "get_registry",
    "register_backend",
    "reset_registry",
    "list_backends",
]
