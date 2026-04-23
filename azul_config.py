"""
Azul Payment Gateway — Configuration loader (legacy standalone version).

This is a thin wrapper around the app-level config for backward compatibility
with standalone scripts like smoke_test.py and azul_client.py.
"""

from app.infrastructure.azul_config import AzulConfig, load_azul_config

__all__ = ["AzulConfig", "load_azul_config"]
