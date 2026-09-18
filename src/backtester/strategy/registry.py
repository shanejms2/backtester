"""Name → Strategy class. Built-ins register on import; extras via importlib."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from pathlib import Path

from backtester.exceptions import InvalidSpecError, UnknownStrategyError
from backtester.strategy.interface import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """Register a Strategy class under ``name``.

    Args:
        name: Registry key used in the Spec (``strategy: buy_hold``).

    Returns:
        Class decorator. Re-registering the same class is a no-op; a different
        class at the same name raises ``ValueError``.
    """

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        existing = _REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"duplicate strategy {name!r}: {existing.__name__} vs {cls.__name__}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get(name: str) -> type[Strategy]:
    """Return the Strategy class for ``name``.

    Args:
        name: Registry key.

    Returns:
        Registered class (instantiate at the call site).

    Raises:
        UnknownStrategyError: Name is not registered.
    """
    _ensure_builtins()
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(list_strategies()) or "(none)"
        raise UnknownStrategyError(f"unknown strategy {name!r}; registered: {known}")
    return cls


def list_strategies() -> list[str]:
    """Return registered Strategy names in sorted order."""
    _ensure_builtins()
    return sorted(_REGISTRY)


def load_plugin(path: str) -> None:
    """Import a module path or ``.py`` file so its ``@register`` runs.

    Args:
        path: Dotted module (``mypkg.strat``) or filesystem path to a ``.py``.

    Raises:
        InvalidSpecError: The module or file cannot be imported.
    """
    candidate = Path(path)
    if candidate.suffix == ".py" and candidate.is_file():
        spec = importlib.util.spec_from_file_location(candidate.stem, candidate)
        if spec is None or spec.loader is None:
            raise InvalidSpecError(f"cannot load plugin {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return
    try:
        importlib.import_module(path)
    except Exception as exc:
        raise InvalidSpecError(f"cannot load plugin {path}: {exc}") from exc


def _ensure_builtins() -> None:
    """Import seam-prover plugins so ``@register`` has run."""
    from backtester.strategy import plugins as _plugins  # noqa: F401


__all__ = ["get", "list_strategies", "load_plugin", "register"]
