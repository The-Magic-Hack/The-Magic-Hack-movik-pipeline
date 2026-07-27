"""
movik/load_to_supabase.py
=========================
Carga movik.db → Supabase, schema "movik".

Tablas que escribe:
  movik.carriers     espejo 1:1 de la tabla carriers de movik.db
  movik.ucc_filings  espejo 1:1 de la tabla ucc_filings de movik.db
  movik.maestro      el join final carriers × scrape_log × filing representativo

Requisitos previos (una sola vez):
  1. pip install supabase
  2. Correr supabase_schema.sql en el SQL Editor de Supabase.
     El schema y las tablas NO los crea este script: supabase-py habla con
     PostgREST, que es una API de datos y no ejecuta DDL.
  3. Supabase → Settings → API → "Exposed schemas" → agregar  movik.
     Sin eso PostgREST responde 404 aunque las tablas existan.

Credenciales (variables de entorno o archivo .env junto a este script):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  la service_role key (NO la anon)

Uso:
  python load_to_supabase.py                      # las 3 tablas
  python load_to_supabase.py --only maestro       # solo una
  python load_to_supabase.py --batch 500          # batches más chicos
  python load_to_supabase.py --dry-run            # arma todo, no sube nada

Idempotente: todo se sube con upsert sobre la PK, así que repetir la carga
actualiza en vez de duplicar. Para partir de cero, vuelve a correr
supabase_schema.sql (hace DROP TABLE).

NOTA sobre el filing representativo
-----------------------------------
Un carrier puede tener varios filings. El representativo aquí es el MISMO que
elige Movikapp/src/lib/db.ts: el gravamen activo que vence antes. 03_build_master.py
toma en cambio el primero por id, lo que produce filas donde la prioridad y los
días mostrados vienen de filings distintos. Como esta tabla es la que alimenta
la app, manda el criterio de la app.
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "movik.db"
SCHEMA = "movik"
BATCH_SIZE = 1_000

# Estados con scraper UCC implementado y corrido. Los demás quedan en
# "Sin datos" con una nota que lo explica, nunca en P1 (ver 03_build_master.py).
SCRAPED_STATES = {"FL", "CA"}

# Orden de urgencia de Movikapp (src/lib/queries.ts PRIORITY_RANK).
PRIORITY_RANK = {"P1": 0, "P2b": 1, "P2c": 2, "P2a": 3, "P3": 4, "P4": 5}

try:
    from supabase import create_client
except ImportError:
    print("Falta supabase-py  →  pip install supabase")
    sys.exit(1)

try:  # la ruta del import cambió entre versiones de supabase-py
    from supabase import ClientOptions  # type: ignore
except ImportError:  # pragma: no cover
    from supabase.lib.client_options import ClientOptions  # type: ignore


# ── CREDENCIALES ─────────────────────────────────────────────────────────────
def load_dotenv_if_present():
    """.env mínimo: KEY=VALUE por línea. Sin dependencias extra."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_client():
    load_dotenv_if_present()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()

    if not url or not key:
        print("❌ Faltan credenciales.")
        print("   Define SUPABASE_URL y SUPABASE_SERVICE_KEY (env o movik/.env).")
        sys.exit(1)

    # create_client espera la URL base del proyecto, no el endpoint /rest/v1/.
    for suffix in ("/rest/v1/", "/rest/v1", "/"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break

    return create_client(url, key, options=ClientOptions(schema=SCHEMA))


# ── HELPERS ──────────────────────────────────────────────────────────────────
def to_iso(mmddyyyy: str | None) -> str | None:
    """MM/DD/YYYY → YYYY-MM-DD. Cualquier cosa rara → None."""
    if not mmddyyyy:
        return None
    s = str(mmddyyyy).strip()
    parts = s.split("/")
    if len(parts) != 3:
        return None
    mm, dd, yyyy = parts
    if not (mm.isdigit() and dd.isdigit() and yyyy.isdigit()):
        return None
    try:
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def to_int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def push(client, table: str, rows: list[dict], on_conflict: str,
         batch: int, dry_run: bool):
    """Sube `rows` en batches, con progreso por batch."""
    total = len(rows)
    if total == 0:
        print(f"   (nada que subir a {SCHEMA}.{table})")
        return

    n_batches = (total + batch - 1) // batch
    t0 = time.time()
    done = 0

    for i in range(0, total, batch):
        chunk = rows[i : i + batch]
        idx = i // batch + 1

        if not dry_run:
            for attempt in range(1, 4):
                try:
                    client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
                    break
                except Exception as e:  # noqa: BLE001 — la API puede fallar por red o 429
                    if attempt == 3:
                        print(f"\n❌ Batch {idx}/{n_batches} de {table} falló: {e}")
                        raise
                    wait = 2 ** attempt
                    print(f"\n   ⚠️  batch {idx} falló ({e}); reintento en {wait}s")
                    time.sleep(wait)

        done += len(chunk)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed else 0
        eta = (total - done) / rate if rate else 0
        print(
            f"   [{SCHEMA}.{table}] batch {idx:>4}/{n_batches}  "
            f"{done:>7,}/{total:,} filas  "
            f"{rate:>6.0f} filas/s  ETA {eta/60:>4.1f} min",
            flush=True,
        )

    print(f"   ✅ {SCHEMA}.{table}: {done:,} filas en {(time.time()-t0)/60:.1f} min")


# ── EXTRACCIÓN DESDE movik.db ────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    if not DB_FILE.exists():
        print(f"❌ {DB_FILE} no existe.")
        sys.exit(1)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


CARRIER_COLS = [
    "dot_number", "legal_name", "dba_name", "phy_state", "phy_city", "phy_street",
    "phy_zip", "phone", "cell_phone", "email_address", "company_officer_1",
    "power_units", "truck_units", "fleetsize", "total_drivers", "classdef",
    "carrier_operation", "safety_rating", "mcs150_date", "status_code",
]
INT_CARRIER_COLS = {"power_units", "truck_units", "fleetsize", "total_drivers"}


def extract_carriers(conn) -> list[dict]:
    print("\n📥 Leyendo carriers de movik.db...")
    rows = []
    for r in conn.execute(f"SELECT {', '.join(CARRIER_COLS)} FROM carriers"):
        d = {c: (to_int(r[c]) if c in INT_CARRIER_COLS else r[c]) for c in CARRIER_COLS}
        d["dot_number"] = str(d["dot_number"])
        rows.append(d)
    print(f"   {len(rows):,} carriers")
    return rows


UCC_COLS = [
    "id", "dot_number", "legal_name_searched", "match_found", "alert_priority",
    "ucc_number", "date_filed", "expires_date", "days_to_expiry", "secured_party",
    "secured_party_addr", "secured_party_type", "filing_type", "filing_status",
    "state_registry", "scraped_at",
]


def extract_ucc(conn) -> list[dict]:
    print("\n📥 Leyendo ucc_filings de movik.db...")
    rows = []
    for r in conn.execute(f"SELECT {', '.join(UCC_COLS)} FROM ucc_filings"):
        d = {c: r[c] for c in UCC_COLS}
        d["id"] = int(d["id"])
        d["dot_number"] = str(d["dot_number"])
        d["match_found"] = to_int(d["match_found"]) or 0
        d["days_to_expiry"] = to_int(d["days_to_expiry"])
        rows.append(d)
    print(f"   {len(rows):,} filings")
    return rows


# El filing representativo y la prioridad se calculan con la MISMA lógica que
# Movikapp/src/lib/db.ts (BUILD_CACHE_SQL), para que local y producción muestren
# exactamente los mismos números.
MAESTRO_SQL = """
DROP TABLE IF EXISTS temp.mv_rep;
CREATE TEMP TABLE mv_rep AS
SELECT
  u.dot_number,
  COALESCE(
    (SELECT f.id FROM ucc_filings f
      WHERE f.dot_number = u.dot_number AND f.match_found = 1
        AND LOWER(f.filing_status) IN ('filed', 'active')
        AND f.days_to_expiry IS NOT NULL
      ORDER BY f.days_to_expiry ASC, f.id ASC LIMIT 1),
    (SELECT f.id FROM ucc_filings f
      WHERE f.dot_number = u.dot_number AND f.match_found = 1
        AND LOWER(f.filing_status) IN ('filed', 'active')
      ORDER BY f.id ASC LIMIT 1),
    (SELECT f.id FROM ucc_filings f
      WHERE f.dot_number = u.dot_number AND f.match_found = 1
      ORDER BY f.days_to_expiry DESC, f.id ASC LIMIT 1)
  ) AS rep_id,
  COUNT(*) AS n_filings,
  SUM(CASE WHEN LOWER(u.filing_status) = 'filed'  THEN 1 ELSE 0 END) AS n_filed,
  SUM(CASE WHEN LOWER(u.filing_status) = 'lapsed' THEN 1 ELSE 0 END) AS n_lapsed
FROM ucc_filings u
WHERE u.match_found = 1
GROUP BY u.dot_number;
"""

MAESTRO_SELECT = """
SELECT
  c.dot_number, c.legal_name, c.dba_name, c.phy_state, c.phy_city, c.phy_street,
  c.phy_zip, c.phone, c.cell_phone, c.email_address, c.company_officer_1,
  c.power_units, c.truck_units, c.fleetsize, c.total_drivers,
  c.classdef, c.carrier_operation, c.safety_rating, c.mcs150_date,
  s.status  AS log_status,
  s.error_msg AS log_error,
  s.scraped_at AS log_scraped_at,
  r.rep_id, r.n_filings, r.n_filed, r.n_lapsed,
  f.alert_priority, f.ucc_number, f.date_filed, f.expires_date, f.days_to_expiry,
  f.secured_party, f.secured_party_type, f.filing_status, f.state_registry
FROM carriers c
LEFT JOIN scrape_log s ON s.dot_number = c.dot_number
LEFT JOIN mv_rep     r ON r.dot_number = c.dot_number
LEFT JOIN ucc_filings f ON f.id = r.rep_id
"""


def build_maestro(conn) -> list[dict]:
    print("\n📥 Construyendo el maestro (carriers × scrape_log × UCC)...")
    conn.executescript(MAESTRO_SQL)

    rows = []
    for r in conn.execute(MAESTRO_SELECT):
        state = (r["phy_state"] or "").strip()
        status = r["log_status"]

        # Misma regla que db.ts y 03_build_master.py:
        # sin fila en scrape_log el carrier NUNCA se consultó → "Sin datos", no P1.
        if status == "error":
            priority = "Error"
        elif status == "not_found":
            priority = "P1"
        elif status == "found":
            # 'found' sin filing representativo no debería pasar; si pasa, es un
            # dato roto, no un P1 (inventarlo crearía un lead falso).
            priority = "Error" if r["rep_id"] is None else (r["alert_priority"] or "P4")
        else:
            priority = "Sin datos"

        if priority == "Error":
            ucc_found = "Error"
        elif priority == "P1":
            ucc_found = "No"
        elif priority == "Sin datos":
            ucc_found = f"Sin datos ({state})" if state else "Sin datos"
        else:
            ucc_found = "Sí"

        if priority == "Error" and status == "error":
            notes = (r["log_error"] or "")[:200]
        elif priority == "Error":
            notes = "found sin filings en ucc_filings"
        elif priority == "Sin datos":
            notes = "" if state in SCRAPED_STATES else f"{state} sin scraper UCC"
        elif (r["n_filings"] or 0) > 1:
            notes = f"{r['n_filings']} filings ({r['n_filed']} filed, {r['n_lapsed']} lapsed)"
        else:
            notes = ""

        power = to_int(r["power_units"])
        truck = to_int(r["truck_units"])

        rows.append({
            "dot_number": str(r["dot_number"]),
            "legal_name": r["legal_name"],
            "dba_name": r["dba_name"],
            "phy_state": state,
            "phy_city": r["phy_city"],
            "phy_street": r["phy_street"],
            "phy_zip": r["phy_zip"],
            "phone": r["phone"],
            "cell_phone": r["cell_phone"],
            "email_address": r["email_address"],
            "company_officer_1": r["company_officer_1"],
            "power_units": power,
            "truck_units": truck,
            "fleetsize": to_int(r["fleetsize"]),
            "total_drivers": to_int(r["total_drivers"]),
            "units": power if power else (truck if truck else 0),
            "classdef": r["classdef"],
            "carrier_operation": r["carrier_operation"],
            "safety_rating": r["safety_rating"],
            "mcs150_date": r["mcs150_date"],
            "priority": priority,
            "prio_rank": PRIORITY_RANK.get(priority, 9),
            "ucc_found": ucc_found,
            "ucc_number": r["ucc_number"] or None,
            "date_filed": r["date_filed"] or None,
            "expires_date": r["expires_date"] or None,
            "expires_date_iso": to_iso(r["expires_date"]),
            "days_to_expiry": to_int(r["days_to_expiry"]),
            "secured_party": r["secured_party"] or None,
            "secured_party_type": r["secured_party_type"] or None,
            "filing_status": r["filing_status"] or None,
            "state_registry": r["state_registry"] or None,
            "n_filings": to_int(r["n_filings"]) or 0,
            "scraped_at": r["log_scraped_at"],
            "notes": notes,
            "search_name": (r["legal_name"] or "").upper(),
        })

    # Resumen igual al que imprime 03_build_master.py, para poder compararlos.
    from collections import Counter
    by_state = Counter(x["phy_state"] for x in rows)
    by_prio = Counter(x["priority"] for x in rows)
    print(f"   {len(rows):,} filas de maestro")
    print("   por estado:   " + "  ".join(f"{k}={v:,}" for k, v in sorted(by_state.items())))
    print("   por prioridad:" + "  ".join(
        f" {k}={by_prio[k]:,}" for k in
        ["P1", "P2a", "P2b", "P2c", "P3", "P4", "Error", "Sin datos"] if by_prio.get(k)))
    return rows


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Movik → Supabase (schema movik)")
    ap.add_argument("--only", nargs="+", metavar="T",
                    choices=["carriers", "ucc_filings", "maestro"],
                    help="Cargar solo estas tablas")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE, metavar="N")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extrae y arma todo pero no escribe en Supabase")
    args = ap.parse_args()

    targets = args.only or ["carriers", "ucc_filings", "maestro"]

    print(f"\n🚀 Movik → Supabase  ·  schema \"{SCHEMA}\"")
    print(f"   Origen:  {DB_FILE}")
    print(f"   Tablas:  {', '.join(targets)}")
    print(f"   Batch:   {args.batch:,} filas")
    if args.dry_run:
        print("   ⚠️  DRY RUN: no se escribe nada")

    client = None if args.dry_run else get_client()
    conn = open_db()
    t0 = time.time()

    try:
        if "carriers" in targets:
            push(client, "carriers", extract_carriers(conn), "dot_number",
                 args.batch, args.dry_run)

        if "ucc_filings" in targets:
            push(client, "ucc_filings", extract_ucc(conn), "id",
                 args.batch, args.dry_run)

        if "maestro" in targets:
            push(client, "maestro", build_maestro(conn), "dot_number",
                 args.batch, args.dry_run)
    finally:
        conn.close()

    print(f"\n✅ Listo en {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
