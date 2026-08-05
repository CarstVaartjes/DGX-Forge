"""Production worker handler boundary.

Node mutation is executed only by authenticated outbound agents. The control
worker therefore has no local process-backed node handlers.
"""

from __future__ import annotations

from collections.abc import Mapping


def production_handlers() -> Mapping[str, object]:
    """Return the deliberately empty production direct-handler registry."""

    return {}
