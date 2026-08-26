"""Verification — never assume a tool succeeded (engineering rule 13)."""

from sali.verify.engine import command_ok, output_present

__all__ = ["command_ok", "output_present"]
