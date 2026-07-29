"""
movik/gen_supabase_schema.py
============================
Reescribe los bloques CREATE TABLE de movik.carriers y movik.maestro dentro de
supabase_schema.sql, generándolos desde census_schema.py.

Son 145 y 162 columnas. Mantenerlas a mano en el .sql y sincronizadas con los
scripts de Python no es realista: basta una diferencia de orden o de tipo para
que la carga meta valores en la columna equivocada.

El resto del .sql (índices, vistas, materializadas, grants, RLS) no se toca.

  python gen_supabase_schema.py
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from census_schema import CARRIER_COLS, INT_COLS
from load_to_supabase import MAESTRO_COLS

SQL = Path(__file__).with_name("supabase_schema.sql")

# Tipos de las columnas que NO vienen del censo. Estas sí caben en integer:
# las calcula el loader y están acotadas, no vienen del formulario de FMCSA.
EXTRA_TYPES = {
    "units": "integer", "prio_rank": "smallint", "n_filings": "integer",
    "days_to_expiry": "integer", "expires_date_iso": "date",
}


def pg_type(col: str) -> str:
    if col in EXTRA_TYPES:
        return EXTRA_TYPES[col]
    # bigint para lo que viene del censo: ver la nota en census_schema.sql_type.
    return "bigint" if col in INT_COLS else "text"


def ddl(table: str, cols: list, comentario: str) -> str:
    ancho = max(len(c) for c in cols) + 2
    lineas = []
    for c in cols:
        tipo = pg_type(c)
        if c == "dot_number":
            tipo += " PRIMARY KEY"
        lineas.append(f'    "{c}"'.ljust(ancho + 5) + tipo)
    return (f"-- {comentario}\n"
            f"DROP TABLE IF EXISTS movik.{table} CASCADE;\n"
            f"CREATE TABLE movik.{table} (\n"
            + ",\n".join(lineas) + "\n);")


def reemplazar(texto: str, table: str, nuevo: str) -> str:
    """Cambia el bloque DROP+CREATE de una tabla, respetando lo de alrededor."""
    patron = re.compile(
        rf"-- [^\n]*\nDROP TABLE IF EXISTS movik\.{table} CASCADE;\n"
        rf"CREATE TABLE movik\.{table} \(.*?\n\);",
        re.S,
    )
    if not patron.search(texto):
        print(f"❌ No encontré el bloque de movik.{table} en {SQL.name}")
        sys.exit(1)
    return patron.sub(lambda _: nuevo, texto, count=1)


def main():
    texto = SQL.read_text(encoding="utf-8")

    texto = reemplazar(texto, "carriers", ddl(
        "carriers", CARRIER_COLS,
        f"carriers — espejo del censo FMCSA ({len(CARRIER_COLS)} columnas).\n"
        "-- Generado por gen_supabase_schema.py desde census_schema.py: no editar a mano.\n"
        "-- Se guardan todas las del censo menos phy_barrio y mail_barrio, 100% vacías."))

    texto = reemplazar(texto, "maestro", ddl(
        "maestro", MAESTRO_COLS,
        f"maestro — el censo completo cruzado con UCC ({len(MAESTRO_COLS)} columnas).\n"
        "-- Generado por gen_supabase_schema.py: no editar a mano.\n"
        "-- Una fila por carrier. Las 145 del censo, más lo que sale del cruce."))

    SQL.write_text(texto, encoding="utf-8")
    print(f"✅ {SQL.name} actualizado")
    print(f"   movik.carriers  {len(CARRIER_COLS):>3} columnas")
    print(f"   movik.maestro   {len(MAESTRO_COLS):>3} columnas")


if __name__ == "__main__":
    main()
