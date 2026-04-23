"""
Azul Payment Gateway — Configuration loader.

Supports TWO modes:

  1. **AWS Secrets Manager** (production / EC2)
     Reads from three secrets:
       - iamatlas/azul/dev/api-credentials  → JSON with merchant_id, auth_splitit, auth_3dsecure
       - iamatlas/azul/dev/cert-pem         → PEM certificate body
       - iamatlas/azul/dev/cert-key         → PEM private key body

  2. **Local / .env** (development)
     Set ``AZUL_LOCAL_MODE=1`` and provide env vars or a ``.env`` file with:
       - AZUL_MERCHANT_ID
       - AZUL_AUTH1       (used for both splitit and 3dsecure when specific ones are absent)
       - AZUL_AUTH2
       - AZUL_AUTH1_SPLITIT / AZUL_AUTH2_SPLITIT   (optional overrides)
       - AZUL_AUTH1_3DS    / AZUL_AUTH2_3DS         (optional overrides)
       - AZUL_CERT_PATH   (path to .crt file)
       - AZUL_KEY_PATH    (path to .key file)
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load a .env file from project root into os.environ (if it exists)."""
    # Walk up from this file to find the project root .env
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)

_load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCAL_MODE = os.getenv("AZUL_LOCAL_MODE", "0") == "1"

_REGION = os.getenv("AZUL_AWS_REGION", "us-east-2")
_SECRET_CREDS = os.getenv("AZUL_SECRET_CREDS", "iamatlas/azul/dev/api-credentials")
_SECRET_CERT = os.getenv("AZUL_SECRET_CERT", "iamatlas/azul/dev/cert-pem")
_SECRET_KEY = os.getenv("AZUL_SECRET_KEY", "iamatlas/azul/dev/cert-key")

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzulConfig:
    """Immutable holder for everything the Azul client needs."""

    cert_path: str
    key_path: str
    merchant_id: str
    auth_splitit: tuple[str, str]   # (auth1, auth2) — ventas simples, sin 3DS
    auth_3dsecure: tuple[str, str]  # (auth1, auth2) — flujo 3D Secure 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_secret(name: str, region: str = _REGION) -> str:
    """Fetch a plain-text secret from AWS Secrets Manager."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    return resp["SecretString"]


def _write_temp_pem(content: str, suffix: str) -> str:
    """Write PEM content to a named temp file and return its path.

    The file is intentionally *not* deleted on close so that httpx can
    read it later.  It will be cleaned up when the OS reclaims temp space
    or when the process exits.
    """
    fd = tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, mode="w", encoding="utf-8"
    )
    fd.write(content)
    fd.flush()
    fd.close()
    return fd.name


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_from_aws() -> AzulConfig:
    """Build config by pulling secrets from AWS Secrets Manager."""
    creds_json = _get_secret(_SECRET_CREDS)
    cert_pem = _get_secret(_SECRET_CERT)
    key_pem = _get_secret(_SECRET_KEY)

    creds = json.loads(creds_json)

    return AzulConfig(
        cert_path=_write_temp_pem(cert_pem, ".crt"),
        key_path=_write_temp_pem(key_pem, ".key"),
        merchant_id=creds["merchant_id"],
        auth_splitit=(
            creds["auth_splitit"]["auth1"],
            creds["auth_splitit"]["auth2"],
        ),
        auth_3dsecure=(
            creds["auth_3dsecure"]["auth1"],
            creds["auth_3dsecure"]["auth2"],
        ),
    )


def _load_from_env() -> AzulConfig:
    """Build config from environment variables / .env file."""
    merchant_id = os.environ.get("AZUL_MERCHANT_ID", "")
    if not merchant_id:
        raise RuntimeError(
            "AZUL_LOCAL_MODE=1 but AZUL_MERCHANT_ID is not set. "
            "Provide credentials via .env or environment variables."
        )

    # Auth — allow specific overrides or a single pair for both modes
    auth1_default = os.environ.get("AZUL_AUTH1", "")
    auth2_default = os.environ.get("AZUL_AUTH2", "")

    auth1_splitit = os.environ.get("AZUL_AUTH1_SPLITIT", auth1_default)
    auth2_splitit = os.environ.get("AZUL_AUTH2_SPLITIT", auth2_default)
    auth1_3ds = os.environ.get("AZUL_AUTH1_3DS", auth1_default)
    auth2_3ds = os.environ.get("AZUL_AUTH2_3DS", auth2_default)

    cert_path = os.environ.get("AZUL_CERT_PATH", "")
    key_path = os.environ.get("AZUL_KEY_PATH", "")

    if not cert_path or not key_path:
        raise RuntimeError(
            "AZUL_LOCAL_MODE=1 but AZUL_CERT_PATH and/or AZUL_KEY_PATH "
            "are not set. Point them to your .crt and .key PEM files."
        )

    return AzulConfig(
        cert_path=cert_path,
        key_path=key_path,
        merchant_id=merchant_id,
        auth_splitit=(auth1_splitit, auth2_splitit),
        auth_3dsecure=(auth1_3ds, auth2_3ds),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_azul_config() -> AzulConfig:
    """Build an AzulConfig from local env or AWS depending on AZUL_LOCAL_MODE.

    Results are cached for the lifetime of the process so that we don't
    hit Secrets Manager on every request.
    """
    if _LOCAL_MODE:
        return _load_from_env()
    return _load_from_aws()
