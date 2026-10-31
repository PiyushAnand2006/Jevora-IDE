"""Browser control subsystem.

Integrated with the existing screen_control module and the VM infrastructure:
  - backend/browser_control/      new module (this package)
  - backend/browser_control/api/  REST endpoints
  - backend/browser_control/tools/browser_control_tool.py  tool for agents
  - backend/db/models.py          BrowserSession table (auto-migrated by Alembic)

Enables the pattern found in Antigravity.exe: a remote-control / browser-automation
service driven by the IDE's agent loop, with per-user sessions bound to VMs and an
allowed-hosts gate on every navigation/interaction.
"""

from backend.browser_control.models import (
    BrowserAction,
    BrowserActionResult,
    BrowserScreenshot,
    BrowserSessionCreate,
    BrowserSessionInfo,
    BrowserSessionUpdate,
)
from backend.browser_control.session import SessionManager

__all__ = [
    "BrowserAction",
    "BrowserActionResult",
    "BrowserScreenshot",
    "BrowserSessionCreate",
    "BrowserSessionInfo",
    "BrowserSessionUpdate",
    "SessionManager",
]
