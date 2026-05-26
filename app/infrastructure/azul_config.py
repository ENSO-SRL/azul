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
       - AZUL_ENV         sandbox | production  (default: sandbox)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

# Keys that the .env file must always win over stale OS-level env vars.
# Using setdefault for everything else preserves Docker/ECS injection.
_DOTENV_OVERRIDE_KEYS = frozenset({
    "AZUL_ENV",
    "AZUL_LOCAL_MODE",
})


def _load_dotenv() -> None:
    """Load a .env file from project root into os.environ (if it exists).

    Supports multi-line PEM values: a value that starts with
    '-----BEGIN' continues until a line starting with '-----END'.

    Critical keys listed in ``_DOTENV_OVERRIDE_KEYS`` are set with direct
    assignment so the .env file always wins over stale OS-level values.
    All other keys use ``setdefault`` to preserve Docker / ECS injection.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    with open(env_path, encoding="utf-8") as fh:
        lines = fh.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        i += 1

        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")

        # Multi-line PEM detection: value starts with "-----BEGIN"
        if value.startswith("-----BEGIN"):
            pem_lines = [value]
            while i < len(lines):
                next_line = lines[i].rstrip("\r\n")
                i += 1
                pem_lines.append(next_line.strip())
                if next_line.strip().startswith("-----END"):
                    break
            value = "\n".join(pem_lines)

        # Critical keys: .env always wins (override stale OS env).
        # All others: only set if not already present (preserve ECS injection).
        if key in _DOTENV_OVERRIDE_KEYS:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)

_load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCAL_MODE = os.getenv("AZUL_LOCAL_MODE", "0") == "1"

_REGION       = os.getenv("AZUL_AWS_REGION",  "us-east-2")
_SECRET_CREDS = os.getenv("AZUL_SECRET_CREDS", "iamatlas/azul/dev/api-credentials")
_SECRET_CERT  = os.getenv("AZUL_SECRET_CERT",  "iamatlas/azul/dev/cert-pem")
_SECRET_KEY   = os.getenv("AZUL_SECRET_KEY",   "iamatlas/azul/dev/cert-key")

# ---------------------------------------------------------------------------
# Azul API URLs — configurables via env vars (defaults = valores oficiales AZUL)
# ---------------------------------------------------------------------------

# Endpoint principal de pagos
AZUL_URL_SANDBOX    = os.getenv(
    "AZUL_URL_SANDBOX",
    "https://pruebas.azul.com.do/webservices/JSON/default.aspx",
)
AZUL_URL_PRODUCTION = os.getenv(
    "AZUL_URL_PRODUCTION",
    "https://pagos.azul.com.do/WebServices/JSON/default.aspx",
)
# Failover URL (producción) — doc AZUL p.14
AZUL_URL_PRODUCTION_SECONDARY = os.getenv(
    "AZUL_URL_PRODUCTION_SECONDARY",
    "https://contpagos.azul.com.do/Webservices/JSON/default.aspx",
)

# 3DS 2.0 — processthreedsmethod
AZUL_3DS_METHOD_URL_SANDBOX = os.getenv(
    "AZUL_3DS_METHOD_URL_SANDBOX",
    "https://pruebas.azul.com.do/webservices/JSON/default.aspx?processthreedsmethod",
)
AZUL_3DS_METHOD_URL_PROD = os.getenv(
    "AZUL_3DS_METHOD_URL_PROD",
    "https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedsmethod",
)

# 3DS 2.0 — processthreedschallenge
AZUL_3DS_CHALLENGE_URL_SANDBOX = os.getenv(
    "AZUL_3DS_CHALLENGE_URL_SANDBOX",
    "https://pruebas.azul.com.do/webservices/JSON/default.aspx?processthreedschallenge",
)
AZUL_3DS_CHALLENGE_URL_PROD = os.getenv(
    "AZUL_3DS_CHALLENGE_URL_PROD",
    "https://pagos.azul.com.do/WebServices/JSON/default.aspx?processthreedschallenge",
)

# Verify Payment
AZUL_VERIFY_PAYMENT_URL_SANDBOX = os.getenv(
    "AZUL_VERIFY_PAYMENT_URL_SANDBOX",
    "https://pruebas.azul.com.do/webservices/JSON/default.aspx?verifypayment",
)
AZUL_VERIFY_PAYMENT_URL_PROD = os.getenv(
    "AZUL_VERIFY_PAYMENT_URL_PROD",
    "https://pagos.azul.com.do/WebServices/JSON/default.aspx?verifypayment",
)

# URL base de la aplicación (para construir TermUrl y MethodNotificationUrl)
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

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
    env: Literal["sandbox", "production"] = "sandbox"

    @property
    def api_url(self) -> str:
        """Return the correct Azul API URL for the configured environment."""
        return AZUL_URL_PRODUCTION if self.env == "production" else AZUL_URL_SANDBOX

    @property
    def threeds_method_url(self) -> str:
        return AZUL_3DS_METHOD_URL_PROD if self.env == "production" else AZUL_3DS_METHOD_URL_SANDBOX

    @property
    def threeds_challenge_url(self) -> str:
        return AZUL_3DS_CHALLENGE_URL_PROD if self.env == "production" else AZUL_3DS_CHALLENGE_URL_SANDBOX

    @property
    def verify_payment_url(self) -> str:
        return AZUL_VERIFY_PAYMENT_URL_PROD if self.env == "production" else AZUL_VERIFY_PAYMENT_URL_SANDBOX

    @property
    def app_base_url(self) -> str:
        return APP_BASE_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_secret(name: str, region: str = _REGION) -> str:
    """Fetch a plain-text secret from AWS Secrets Manager."""
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=name)
    return resp["SecretString"]


def _normalize_pem(content: str) -> str:
    """Reconstruct a valid PEM string from a potentially space-flattened value.

    ECS (and many secret managers) store multi-line env vars as a single line
    with spaces replacing newlines, e.g.:

        "-----BEGIN CERTIFICATE----- MIIH...base64... -----END CERTIFICATE-----"

    OpenSSL / Python ssl requires proper newlines and 64-char base64 wrapping.
    This function detects that case and rebuilds the PEM correctly.
    """
    import re as _re

    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    # If it already has newlines after the header, it's fine
    if "\n" in content:
        return content

    # Space-flattened PEM — reconstruct it
    # Match: -----BEGIN ...----- <base64 with spaces> -----END ...-----
    match = _re.match(
        r"(-----BEGIN [^-]+-----)\s+(.*?)\s+(-----END [^-]+-----)",
        content,
        _re.DOTALL,
    )
    if not match:
        # Can't parse — return as-is and let ssl give a clear error
        return content

    header, b64_body, footer = match.group(1), match.group(2), match.group(3)
    # Remove all whitespace from the body, then wrap in 64-char lines
    b64_clean = "".join(b64_body.split())
    b64_lines = "\n".join(b64_clean[i:i+64] for i in range(0, len(b64_clean), 64))
    return f"{header}\n{b64_lines}\n{footer}\n"


def _write_temp_pem(content: str, suffix: str) -> str:
    """Write PEM content to a *stable*, fixed-name temp file and return its path.

    Uses a deterministic path so the lru_cache always points to the same file
    and it is never garbage-collected between requests (which caused the
    ``ssl.SSLError: [SSL] PEM lib`` crash with random NamedTemporaryFile paths).

    Also normalizes PEM that arrives space-flattened from ECS env vars.
    """
    stable_name = f"azul_cert{suffix}"
    path = Path(tempfile.gettempdir()) / stable_name
    path.write_text(_normalize_pem(content), encoding="utf-8", newline="\n")
    return str(path)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_from_aws() -> AzulConfig:
    """Build config by pulling secrets from AWS Secrets Manager."""
    creds_json = _get_secret(_SECRET_CREDS)
    cert_pem   = _get_secret(_SECRET_CERT)
    key_pem    = _get_secret(_SECRET_KEY)

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
        env=creds.get("env", "production"),
    )


def _load_from_env() -> AzulConfig:
    """Build config from environment variables / .env file.

    Certificate resolution order (first match wins):
      1. AZUL_CERT_PEM + AZUL_KEY_PEM  → PEM content written to temp files
      2. AZUL_CERT_PATH + AZUL_KEY_PATH → file paths (development only)
    """
    merchant_id = os.environ.get("AZUL_MERCHANT_ID", "")
    if not merchant_id:
        raise RuntimeError(
            "AZUL_LOCAL_MODE=1 but AZUL_MERCHANT_ID is not set. "
            "Provide credentials via .env or environment variables."
        )

    auth1_default = os.environ.get("AZUL_AUTH1", "")
    auth2_default = os.environ.get("AZUL_AUTH2", "")

    auth1_splitit = os.environ.get("AZUL_AUTH1_SPLITIT", auth1_default)
    auth2_splitit = os.environ.get("AZUL_AUTH2_SPLITIT", auth2_default)
    auth1_3ds     = os.environ.get("AZUL_AUTH1_3DS", auth1_default)
    auth2_3ds     = os.environ.get("AZUL_AUTH2_3DS", auth2_default)

    # -----------------------------------------------------------------------
    # Certificate resolution — priority order:
    #   1. AZUL_CERT_PATH / AZUL_KEY_PATH  (physical files — validated matching pair)
    #   2. AZUL_CERT_PEM / AZUL_KEY_PEM    (inline PEM — ECS/Docker fallback)
    # NOTE: The inline PEM in .env may differ from physical files.
    #       Physical files are the authoritative cert+key pair from Luis Recio.
    # -----------------------------------------------------------------------
    import pathlib
    cert_path = os.environ.get("AZUL_CERT_PATH", "").strip()
    key_path  = os.environ.get("AZUL_KEY_PATH",  "").strip()

    if cert_path and key_path and pathlib.Path(cert_path).is_file() and pathlib.Path(key_path).is_file():
        # Use physical files directly — they are the validated matching pair
        pass
    else:
        # Fall back to inline PEM (ECS / Docker — no physical files available)
        cert_pem = os.environ.get("AZUL_CERT_PEM", "").strip()
        key_pem  = os.environ.get("AZUL_KEY_PEM",  "").strip()

        if cert_pem and key_pem:
            cert_path = _write_temp_pem(cert_pem, ".crt")
            key_path  = _write_temp_pem(key_pem, ".key")
        else:
            raise RuntimeError(
                "Certificates not configured. Provide one of:\n"
                "  • AZUL_CERT_PATH + AZUL_KEY_PATH  (physical files — local dev)\n"
                "  • AZUL_CERT_PEM  + AZUL_KEY_PEM   (inline PEM — ECS/Docker)"
            )

    env: Literal["sandbox", "production"] = (
        "production" if os.environ.get("AZUL_ENV", "sandbox") == "production" else "sandbox"
    )

    return AzulConfig(
        cert_path=cert_path,
        key_path=key_path,
        merchant_id=merchant_id,
        auth_splitit=(auth1_splitit, auth2_splitit),
        auth_3dsecure=(auth1_3ds, auth2_3ds),
        env=env,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _env_has_all_credentials() -> bool:
    """Return True when all Azul credentials are already injected as env vars.

    When ECS injects secrets directly into env vars (AZUL_MERCHANT_ID,
    AZUL_AUTH1/2, AZUL_CERT_PEM + AZUL_KEY_PEM), there is no need to hit
    AWS Secrets Manager.  Calling Secrets Manager without proper IAM permissions
    produces the misleading ``Unable to locate credentials`` error even though
    every credential the app needs is already in the process environment.
    """
    has_merchant = bool(os.environ.get("AZUL_MERCHANT_ID", "").strip())
    has_auth     = bool(os.environ.get("AZUL_AUTH1", "").strip())

    # Inline PEM (ECS / Docker)
    has_pem = (
        bool(os.environ.get("AZUL_CERT_PEM", "").strip()) and
        bool(os.environ.get("AZUL_KEY_PEM",  "").strip())
    )
    # Physical cert files (local dev)
    import pathlib
    cert_path = os.environ.get("AZUL_CERT_PATH", "").strip()
    key_path  = os.environ.get("AZUL_KEY_PATH",  "").strip()
    has_files = (
        bool(cert_path) and bool(key_path) and
        pathlib.Path(cert_path).is_file() and pathlib.Path(key_path).is_file()
    )

    return has_merchant and has_auth and (has_pem or has_files)


@lru_cache(maxsize=1)
def _load_azul_config_cached() -> AzulConfig:
    """Internal cached loader — do not call directly. Use ``load_azul_config()``."""
    if _LOCAL_MODE:
        return _load_from_env()

    # Fast-path: skip Secrets Manager when credentials are already in env vars.
    # This is the normal production path when ECS injects secrets directly
    # (as opposed to referencing AWS Secrets Manager ARNs in the task def).
    if _env_has_all_credentials():
        logger.info(
            "[azul_config] All credentials found in env vars — "
            "skipping AWS Secrets Manager (AZUL_LOCAL_MODE=0, direct injection)."
        )
        return _load_from_env()

    return _load_from_aws()


def load_azul_config() -> AzulConfig:
    """Return an AzulConfig, reloading from source when stale.

    The ``lru_cache`` on the inner function avoids hitting Secrets Manager on
    every request.  This wrapper busts the cache in two scenarios:

    1. **Missing cert files** — OS reclaimed the temp files written during a
       previous load (memory pressure or ECS task restart).  Prevents the
       ``ssl.SSLError: [SSL] PEM lib`` crash.

    2. **Env mismatch** — The cached ``cfg.env`` no longer matches the current
       ``AZUL_ENV`` env var (e.g. .env was changed from sandbox→production
       without a full process restart).  Prevents ``VALIDATION_ERROR:ForceNo3DS``
       from being sent to the production gateway because the cache still held
       a sandbox config that included ``ForceNo3DS: "1"``.
    """
    cfg = _load_azul_config_cached()

    # Guard 1: bust cache if cert files have vanished.
    certs_ok = Path(cfg.cert_path).is_file() and Path(cfg.key_path).is_file()
    if not certs_ok:
        logger.warning(
            "[azul_config] Cert file(s) missing (cert_exists=%s, key_exists=%s) — "
            "busting lru_cache and reloading from source.",
            Path(cfg.cert_path).is_file(),
            Path(cfg.key_path).is_file(),
        )
        _load_azul_config_cached.cache_clear()
        cfg = _load_azul_config_cached()
        return cfg

    # Guard 2: bust cache if AZUL_ENV changed since the cache was populated.
    # This catches the "server started on sandbox, .env later set to production"
    # scenario that caused VALIDATION_ERROR:ForceNo3DS on the production endpoint.
    current_env = "production" if os.environ.get("AZUL_ENV", "sandbox") == "production" else "sandbox"
    if cfg.env != current_env:
        logger.warning(
            "[azul_config] Cached env=%r but AZUL_ENV=%r — busting lru_cache.",
            cfg.env, current_env,
        )
        _load_azul_config_cached.cache_clear()
        cfg = _load_azul_config_cached()

    return cfg
