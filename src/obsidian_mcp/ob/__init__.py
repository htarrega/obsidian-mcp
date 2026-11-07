"""Optional Obsidian Headless (``ob``) sync supervisor and MCP tools.

Nothing in this package runs at import time: ``server.py`` only imports it
when ``ENABLE_OB`` is truthy, then calls :func:`register_ob_tools` (tool
registration) and, from ``main()``, :func:`start_ob` (launches the
``ob sync --continuous`` child). See the README section "ob CLI wrapper".
"""

from __future__ import annotations

from .service import start_ob, stop_ob
from .tools import register_ob_tools

__all__ = ["register_ob_tools", "start_ob", "stop_ob"]
