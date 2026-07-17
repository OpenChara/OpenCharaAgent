"""Resident desktop supervisor (charad).

The supervisor owns long-lived per-chara stdio children and exposes them to the
web renderer as a thin JSON-RPC pipe. It deliberately never imports core/tools:
chara work happens only inside ``chara serve <name> --stdio`` children.

This is a PACKAGE split by concern (observability / lifestate / children / http
/ core / daemon). Every name that was public on the old flat module is
re-exported here so the historic ``from ..server import supervisor as SUP`` and
``from .supervisor import …`` contracts are byte-identical — including the
stdlib/sibling module re-exports the test-suite reaches for (``SUP.S``,
``SUP.N``, ``SUP.APP_DIR``, ``SUP.WEB_DIR``, ``SUP._DRIVER_SEND_TIMEOUT_SECONDS``).
``_DRIVER_SEND_TIMEOUT_SECONDS`` is patched ON THIS PACKAGE by the slow-client
test; ``children._driver_send_timeout()`` reads it back off the package at call
time so the patch is honored.
"""
from __future__ import annotations

# Sibling modules some callers/tests reach via the package namespace
# (SUP.S monkeypatched for list_sessions; SUP.N.is_wildcard_host in desktop.py).
from ...session import isolation as I  # noqa: F401 - re-exported on the package surface
from ...session import sessions as S  # noqa: F401 - re-exported on the package surface
from .. import authpw as AUTHPW  # noqa: F401 - re-exported on the package surface
from .. import hub as H  # noqa: F401 - re-exported on the package surface
from .. import netsec as N  # noqa: F401 - re-exported on the package surface

from .paths import APP_DIR, UPLOAD_MAX, WEB_DIR  # noqa: F401
from .observability import (  # noqa: F401
    _MEMORY_INTERVAL_SECONDS,
    ResourceCanary,
    _proc_summary,
    _rss_mb,
    _signal_name,
    format_shutdown_context,
    log_memory_usage,
    snapshot_shutdown_context,
)
from .lifestate import (  # noqa: F401
    DriverSlot,
    FrameRing,
    IdleGate,
    LifeState,
    PermanentIdleBackoff,
    RestartBackoff,
    permanent_model_error,
)
from .children import (  # noqa: F401
    GATEWAY_FATAL_EXIT,
    CharaChild,
    GatewayChild,
    GatewayInfo,
    _Driver,
    _DRIVER_SEND_TIMEOUT_SECONDS,
)
from .http import (  # noqa: F401
    WebHandler,
    _PREAUTH_EXACT,
    _PREAUTH_PREFIXES,
    _is_preauth_path,
    _reachable_ips,
    free_port,
    start_http,
)
from .core import _PTY_READ_TIMEOUT, _PTY_RESIZE_RE, Supervisor  # noqa: F401
from .daemon import (  # noqa: F401
    daemon_alive,
    daemon_json_path,
    daemon_log_path,
    daemon_status,
    read_daemon_json,
    stop_daemon_process,
    write_daemon_json,
)
