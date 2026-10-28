"""Free Review: practice every seen card in a deck without rescheduling.

See the repository README for what this does to scheduling and sync.
"""

from __future__ import annotations


def _bootstrap() -> None:
    try:
        from aqt import gui_hooks
    except ImportError:
        return
    from .ui import install_hooks

    install_hooks(gui_hooks)


_bootstrap()
