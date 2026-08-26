"""Sali's exception hierarchy. Errors are never hidden (engineering rule 12)."""

from __future__ import annotations


class SaliError(Exception):
    """Base class for all Sali errors."""


class ConfigError(SaliError):
    """Invalid or missing configuration."""


class DatabaseError(SaliError):
    """Datastore connectivity or integrity failure."""


class MigrationError(DatabaseError):
    """A schema migration could not be applied."""


class ProviderError(SaliError):
    """Model provider (Ollama, etc.) failure."""
