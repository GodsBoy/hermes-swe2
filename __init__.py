"""Devin CLI (SWE-2) model-provider plugin for Hermes Agent.

This file exists so Hermes' provider discovery can import the plugin as a
directory: ``$HERMES_HOME/plugins/<name>/__init__.py`` (``hermes plugins
install`` flat layout) or ``$HERMES_HOME/plugins/model-providers/<name>/``.
All registration logic lives in ``devin_provider.py``.
"""

try:
    from .devin_provider import register
except ImportError:  # pip-installed top-level module path
    from devin_provider import register  # type: ignore[no-redef]

register()
