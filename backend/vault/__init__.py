"""Portable Obsidian vault integration for Jevora run history."""

from backend.vault.obsidian import Vault, VaultError
from backend.vault.service import VaultService

__all__ = ["Vault", "VaultError", "VaultService"]
