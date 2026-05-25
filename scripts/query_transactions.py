"""
Extrae transacciones de la base de datos PostgreSQL (AWS RDS).

Uso:
    py scripts/query_transactions.py
    py scripts/query_transactions.py --limit 100
    py scripts/query_transactions.py --status APPROVED
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Cargar .env desde la raíz del proyecto
_root = Path(__file__).resolve().parents[1]
_env_path = _root / ".env"
if _env_path.is_file():
    with open(_env_path, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k, _v = _k.strip(), _v.strip().strip("\"'")
            if _v.startswith("-----BEGIN"):
                continue
            os.environ.setdefault(_k, _v)

import asyncpg


async def main(limit: int = 50, status: str | None = None) -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres@atlas-user-zadkiel-ohio.cna8kso8qh1g.us-east-2.rds.amazonaws.com:5432/Atlas_User_Service",
    )

    # asyncpg usa postgresql:// (sin +asyncpg)
    dsn = db_url.replace("postgresql+asyncpg://", "postgresql://")

    import ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    print(f"[*] Conectando a PostgreSQL...")
    conn = await asyncpg.connect(dsn=dsn, ssl=ssl_ctx)

    try:
        # Listar tablas en schema pagos
        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'pagos' ORDER BY table_name"
        )
        print("TABLAS EN SCHEMA pagos:", [t["table_name"] for t in tables])
        print()

        # Payments
        where = "WHERE status = $1" if status else ""
        args = [status] if status else []
        query = f"""
            SELECT id, status, iso_code, response_message, azul_order_id,
                   authorization_code, amount, currency, card_number_masked,
                   created_at, updated_at
            FROM pagos.payments
            {where}
            ORDER BY created_at DESC
            LIMIT {limit}
        """
        rows = await conn.fetch(query, *args)
        print(f"=== PAYMENTS ({len(rows)} registros) ===")
        for row in rows:
            print(dict(row))
        print()

        # Transactions
        query_txn = f"""
            SELECT id, payment_id, iso_code, response_code, response_message,
                   http_status, created_at
            FROM pagos.transactions
            ORDER BY created_at DESC
            LIMIT {limit}
        """
        txn_rows = await conn.fetch(query_txn)
        print(f"=== TRANSACTIONS ({len(txn_rows)} registros) ===")
        for row in txn_rows:
            print(dict(row))

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consultar transacciones en PostgreSQL")
    parser.add_argument("--limit", type=int, default=50, help="Máximo de registros a mostrar")
    parser.add_argument("--status", type=str, default=None,
                        help="Filtrar por status: APPROVED, DECLINED, PENDING_3DS, etc.")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, status=args.status))
