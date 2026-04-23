"""
Quick smoke test — validates mTLS + Auth headers against Azul sandbox.

Run:
    py -3 smoke_test.py
"""

import asyncio
import json
import sys

# Fix Unicode output on Windows terminals (cp1252 -> utf-8)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from azul_client import test_connection


async def main() -> None:
    print("[*] Connecting to Azul sandbox (splitit mode)...")
    try:
        result = await test_connection(auth_mode="splitit")
    except Exception as exc:
        print(f"[ERROR] Request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    iso = result.get("IsoCode", "??")
    if iso == "00":
        print("\n[OK] Smoke test PASSED -- IsoCode 00 (Approved)")
    else:
        print(f"\n[WARN] IsoCode {iso} -- review response above")


if __name__ == "__main__":
    asyncio.run(main())
