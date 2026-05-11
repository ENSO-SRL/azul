"""
Extrae todas las transacciones de prueba de la BD local para la certificación AZUL.
Ejecutar: py scripts/query_transactions.py
"""
import sqlite3
import json
from datetime import datetime

DB_PATH = "azul_pagos.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Ver tablas
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cursor.fetchall()]
print("TABLAS EN BD:", tables)
print()

for table in tables:
    print(f"=== TABLA: {table} ===")
    cursor.execute(f"PRAGMA table_info({table})")
    cols = cursor.fetchall()
    col_names = [c[1] for c in cols]
    print("Columnas:", col_names)
    cursor.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 50")
    rows = cursor.fetchall()
    print(f"Registros ({len(rows)}):")
    for row in rows:
        print(dict(row))
    print()

conn.close()
