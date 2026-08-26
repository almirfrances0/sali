"""The local API — a WebSocket that streams Sali's turns (for the future app + realtime UI)."""

from sali.api.app import create_app

__all__ = ["create_app"]
