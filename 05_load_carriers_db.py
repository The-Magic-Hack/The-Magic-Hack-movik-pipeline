"""
movik/05_load_carriers_db.py
============================
Carga el censo de carriers desde census_file_full.csv a la tabla `carriers`
de movik.db.

Por qué existe
--------------
02_transform_export.py genera movik_carriers.xlsx, pero NO toca movik.db.
La única ruta que hoy escribe en la tabla `carriers` está dentro de
ucc_scraper.py (main), y ahí está bloqueada a FL a propósito: correr el
scraper de Florida contra carriers de CA/NC daría datos sin sentido.

Resultado: CA y NC nunca llegan a movik.db, y la webapp (Movikapp) — que lee
la DB, no el xlsx — los reporta en 0.

Este script hace SOLO la carga del censo, sin scrapear nada. Los carriers
entran sin fila en scrape_log, así que la webapp los clasifica como
"Sin datos" — que es lo correcto: nunca se consultó su registro UCC.
Marcarlos P1 los volvería leads falsos.

Uso:
  python 05_load_carriers_db.py --states CA NC
  python 05_load_carriers_db.py --states FL CA NC --dry-run
  python 05_load_carriers_db.py --states CA --replace   # borra ese estado antes
"""

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import duckdb
except ImportError:
    print("Falta duckdb  →  pip install duckdb")
    sys.exit(1)

from census_schema import CARRIER_COLS, INT_COLS, sql_type

CSV_FILE = "census_file_full.csv"
DB_FILE = "movik.db"

# CARRIER_COLS e INT_COLS salen de census_schema: son 145 columnas y tenerlas
# duplicadas aquí era garantía de que tarde o temprano quedaran desalineadas
# con el CREATE TABLE. En el CSV todo viene como texto (all_varchar); las de
# INT_COLS se convierten al insertar.


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_from_csv(csv_path: str, states: list, limit=None) -> list:
    """Mismos filtros que ucc_scraper.load_carriers: activos y con nombre."""
    if not Path(csv_path).exists():
        print(f"No se encuentra {csv_path}. Corre 01_census_incremental.py primero.")
        sys.exit(1)

    st_list = ", ".join(f"'{s}'" for s in states)
    lim = f"LIMIT {limit}" if limit else ""
    q = f"""
        SELECT {', '.join(f'"{c}"' for c in CARRIER_COLS)}
        FROM read_csv_auto('{csv_path}', all_varchar=true,
                           header=true, ignore_errors=true)
        WHERE phy_state IN ({st_list})
          AND status_code = 'A'
          AND legal_name IS NOT NULL
          AND trim(legal_name) <> ''
        ORDER BY mcs150_date DESC NULLS LAST
        {lim}
    """
    con = duckdb.connect()
    rows = con.execute(q).fetchall()
    con.close()
    return rows


def normalize(row: tuple) -> tuple:
    out = []
    for col, val in zip(CARRIER_COLS, row):
        if col in INT_COLS:
            out.append(to_int(val))
        elif col == "legal_name" and val:
            out.append(" ".join(str(val).strip().upper().split()))
        else:
            out.append(None if val in (None, "") else str(val).strip())
    return tuple(out)


def main():
    p = argparse.ArgumentParser(description="Cargar censo de carriers en movik.db")
    p.add_argument("--states", nargs="+", required=True, metavar="ST")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--db", default=DB_FILE)
    p.add_argument("--csv", default=CSV_FILE)
    p.add_argument("--dry-run", action="store_true",
                   help="Cuenta lo que entraría, sin escribir nada")
    p.add_argument("--replace", action="store_true",
                   help="DELETE de esos estados antes de insertar (recarga limpia)")
    args = p.parse_args()

    states = [s.upper() for s in args.states]
    print(f"\nMovik — carga de censo a {args.db}")
    print(f"   Estados: {states}")
    print(f"   Fuente:  {args.csv}\n")

    if not Path(args.db).exists():
        print(f"No existe {args.db}. Corre ucc_scraper.py primero para crear el esquema.")
        sys.exit(1)

    print("Leyendo el censo (duckdb sobre el CSV, tarda)...")
    rows = load_from_csv(args.csv, states, limit=args.limit)
    by_state = {}
    for r in rows:
        by_state[r[3]] = by_state.get(r[3], 0) + 1
    for s in states:
        print(f"   {s}: {by_state.get(s, 0):,} carriers activos en el censo")
    if not rows:
        print("\nEl censo no trae filas para esos estados. Nada que cargar.")
        sys.exit(1)

    conn = sqlite3.connect(args.db)

    # La tabla pudo crearse con las 20 columnas originales. Agrega las que
    # falten sin tocar los datos ya cargados.
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(carriers)")}
    nuevas = [c for c in CARRIER_COLS if c not in existentes]
    for c in nuevas:
        conn.execute(f'ALTER TABLE carriers ADD COLUMN "{c}" {sql_type(c)}')
    if nuevas:
        conn.commit()
        print(f"   esquema: {len(nuevas)} columnas nuevas agregadas a carriers")

    before = {
        s: conn.execute(
            "SELECT COUNT(*) FROM carriers WHERE phy_state = ?", (s,)
        ).fetchone()[0]
        for s in states
    }
    total_before = conn.execute("SELECT COUNT(*) FROM carriers").fetchone()[0]
    print(f"\nEn movik.db ahora: {total_before:,} carriers"
          + "".join(f" · {s}={before[s]:,}" for s in states))

    if args.dry_run:
        print("\n--dry-run: no se escribió nada.")
        conn.close()
        return

    if args.replace:
        for s in states:
            n = conn.execute("DELETE FROM carriers WHERE phy_state = ?", (s,)).rowcount
            print(f"   --replace: {n:,} filas borradas de {s}")

    placeholders = ",".join("?" * len(CARRIER_COLS))
    cols_sql = ", ".join(f'"{c}"' for c in CARRIER_COLS)
    # Upsert, no INSERT OR IGNORE. Con IGNORE, un carrier que ya estaba en la
    # tabla se saltaba entero: al agregar columnas nuevas al esquema, se
    # habrían quedado en NULL para las 532.167 filas ya cargadas, y el script
    # habría reportado "0 nuevos" como si todo estuviera bien.
    updates = ", ".join(f'"{c}" = excluded."{c}"'
                        for c in CARRIER_COLS if c != "dot_number")
    conn.executemany(
        f"INSERT INTO carriers ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(dot_number) DO UPDATE SET {updates}",
        (normalize(r) for r in rows),
    )
    conn.commit()

    total_after = conn.execute("SELECT COUNT(*) FROM carriers").fetchone()[0]
    print(f"\nListo: {total_after:,} carriers en movik.db (+{total_after - total_before:,})")
    print("\nPor estado:")
    for st, n in conn.execute(
        "SELECT phy_state, COUNT(*) FROM carriers GROUP BY phy_state ORDER BY 2 DESC"
    ):
        print(f"   {st or '(vacío)':<8} {n:>9,}")
    conn.close()

    print("\nEn la webapp: pulsa 'Actualizar' (o reinicia npm run dev) para que")
    print("reconstruya su caché y tome los carriers nuevos.")


if __name__ == "__main__":
    main()
