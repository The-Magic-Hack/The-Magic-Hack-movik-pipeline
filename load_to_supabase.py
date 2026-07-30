"""
movik/load_to_supabase.py
=========================
Carga movik.db → Supabase (Postgres), schema "movik".

Tablas que escribe:
  movik.carriers     espejo 1:1 de la tabla carriers de movik.db
  movik.ucc_filings  espejo 1:1 de la tabla ucc_filings de movik.db
  movik.maestro      el join final carriers × scrape_log × filing representativo

¿Por qué psycopg2 y no supabase-py?
-----------------------------------
supabase-py habla con PostgREST, que solo sirve los schemas marcados en
Settings → API → Exposed schemas. Ese ajuste no está disponible en este
proyecto, así que "movik" sería invisible para la API REST. La conexión
directa a Postgres no tiene esa restricción, además de ser bastante más
rápida para cargas de medio millón de filas y de poder crear el schema sola
(PostgREST no ejecuta DDL).

Movikapp sí sigue leyendo por PostgREST con la anon key: para eso
supabase_schema.sql crea vistas public.movik_* que apuntan a movik.*.

Requisitos:
  pip install psycopg2-binary

Credenciales (variable de entorno o archivo .env junto a este script):
  SUPABASE_DB_URL   postgresql://postgres.<ref>:<password>@<host>:5432/postgres

  Se saca de Supabase → Settings → Database → Connection string → URI.
  Usa la de "Session pooler" (puerto 5432): la conexión directa a
  db.<ref>.supabase.co es solo IPv6 y no resuelve desde Windows ni desde los
  runners de GitHub Actions.

Uso:
  python load_to_supabase.py --init-schema      # aplica supabase_schema.sql
  python load_to_supabase.py                    # carga las 3 tablas
  python load_to_supabase.py --only maestro     # solo una
  python load_to_supabase.py --dry-run          # arma todo, no escribe
  python load_to_supabase.py --upsert           # método viejo, ver abajo

CÓMO CARGA
----------
Por defecto: COPY a una tabla de staging, y al final un traslado atómico a la
definitiva. Dos razones.

La primera es que aguanta. Antes se cargaba con 533 INSERT ... ON CONFLICT de
1.000 filas. Con la tabla en 20 columnas funcionaba; al pasarla a 145 saturó la
instancia de Supabase — iba a 1.500 filas/s, se derrumbó a 26 y terminó
rechazando conexiones. COPY es el camino que usa Postgres para carga masiva: una
sentencia, sin evaluar conflictos, sin 533 idas y vueltas por la red, y con
mucho menos WAL.

La segunda es que no deja a la webapp sin datos. Recrear la tabla y cargarla
directo la dejaba en cero filas durante toda la carga. Ahora la parte lenta va a
staging, fuera del camino, y la definitiva se reemplaza en una transacción
corta. Ver load_swap() para por qué no se hace con RENAME.

`--upsert` vuelve al método viejo. Sirve para cargas parciales donde haya que
preservar lo que ya está en la tabla, porque el método nuevo la vacía.

NOTA sobre el filing representativo
-----------------------------------
Un carrier puede tener varios filings. El representativo aquí es el MISMO que
elige Movikapp/src/lib/db.ts: el gravamen activo que vence antes.
03_build_master.py toma en cambio el primero por id, lo que produce filas donde
la prioridad y los días mostrados vienen de filings distintos. Como esta tabla
alimenta la app, manda el criterio de la app.
"""

import argparse
import io
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "movik.db"
SCHEMA_SQL = BASE_DIR / "supabase_schema.sql"
SCHEMA = "movik"
BATCH_SIZE = 1_000

# Estados con scraper UCC implementado y corrido. Los demás quedan en
# "Sin datos" con una nota que lo explica, nunca en P1 (ver 03_build_master.py).
SCRAPED_STATES = {"FL", "CA"}

# Orden de urgencia de Movikapp (src/lib/queries.ts PRIORITY_RANK).
PRIORITY_RANK = {"P1": 0, "P2b": 1, "P2c": 2, "P2a": 3, "P3": 4, "P4": 5}

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("Falta psycopg2  →  pip install psycopg2-binary")
    sys.exit(1)


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


def connect_pg():
    load_dotenv_if_present()
    dsn = (os.environ.get("SUPABASE_DB_URL") or "").strip()
    if not dsn:
        print("❌ Falta SUPABASE_DB_URL (variable de entorno o movik/.env).")
        print("   Supabase → Settings → Database → Connection string → URI")
        print("   Usa la de 'Session pooler', puerto 5432.")
        sys.exit(1)

    # sslmode=require: Supabase rechaza conexiones en claro, y si la URI viene
    # sin el parámetro psycopg2 no lo asume.
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"

    conn = psycopg2.connect(dsn, connect_timeout=30)
    conn.autocommit = False
    with conn.cursor() as cur:
        # Sin límite de tiempo por sentencia: un COPY de 532.167 filas tarda
        # minutos y el statement_timeout que trae el rol lo cancelaba a mitad
        # de camino (murió en la fila 350.572 de la primera prueba). Es seguro
        # acotarlo solo aquí: esta sesión es la del cargador, no la de la app —
        # anon sigue con sus 15s, que es lo que protege a la webapp.
        cur.execute("SET statement_timeout = 0")
        cur.execute("SET idle_in_transaction_session_timeout = 0")
        cur.execute("SELECT current_database(), current_user, version()")
        db, user, ver = cur.fetchone()
    print(f"   Conectado: {db} como {user}")
    print(f"   {ver.split(',')[0]}")
    return conn


# ── HELPERS ──────────────────────────────────────────────────────────────────
def to_iso(mmddyyyy):
    """MM/DD/YYYY → YYYY-MM-DD. Cualquier cosa rara → None."""
    if not mmddyyyy:
        return None
    parts = str(mmddyyyy).strip().split("/")
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


#: Escapes del formato TEXT de COPY. El orden importa: la barra invertida va
#: primero, si no se re-escaparían las que introducen los demás.
_COPY_ESCAPES = (("\\", "\\\\"), ("\t", "\\t"), ("\n", "\\n"), ("\r", "\\r"))


def _copy_field(v) -> str:
    r"""
    Serializa un valor al formato TEXT de COPY.

    Se usa TEXT y no CSV a propósito. En `FORMAT csv` el NULL se representa con
    un campo vacío sin comillas, y `\N` sería la cadena literal de dos
    caracteres: escribir `\N` para los nulos habría metido el texto "\N" en
    cada celda vacía de las 162 columnas, y habría convertido en NULL cada
    cadena vacía legítima. En TEXT, `\N` ES el nulo y la cadena vacía se
    distingue de él, que es justo lo que hace falta.
    """
    if v is None:
        return "\\N"
    s = str(v)
    for viejo, nuevo in _COPY_ESCAPES:
        s = s.replace(viejo, nuevo)
    return s


class _RowsAsCopyText(io.RawIOBase):
    """
    Convierte las filas al formato TEXT de COPY sobre la marcha.

    No se arma el volcado completo en memoria ni en disco: COPY lee de aquí y
    esta clase serializa de a mil filas. Con 532.167 × 162 columnas, el texto
    entero son varios cientos de MB.
    """

    def __init__(self, rows, cols, on_progress=None, every=20_000):
        self.rows, self.cols = rows, cols
        self.on_progress, self.every = on_progress, every
        self.i, self.buf = 0, b""

    def readable(self):
        return True

    def readinto(self, b):
        # Se rellena el buffer hasta cubrir lo que pide el lector.
        while len(self.buf) < len(b) and self.i < len(self.rows):
            hasta = min(self.i + 1000, len(self.rows))
            trozo = "".join(
                "\t".join(_copy_field(r.get(c)) for c in self.cols) + "\n"
                for r in self.rows[self.i : hasta]
            )
            self.buf += trozo.encode("utf-8")
            prev, self.i = self.i, hasta
            if self.on_progress and (hasta // self.every) > (prev // self.every):
                self.on_progress(hasta)

        n = min(len(b), len(self.buf))
        b[:n], self.buf = self.buf[:n], self.buf[n:]
        return n


def copy_into(conn, table: str, cols: list[str], rows: list[dict]) -> None:
    """
    Carga con COPY FROM STDIN. Una sola sentencia en vez de 533 INSERT.

    Por qué: con la tabla en 145 columnas, 533 INSERT ... ON CONFLICT de 1.000
    filas saturaron la instancia — iba a 1.500 filas/s, cayó a 26 y terminó
    dejando de aceptar conexiones. COPY es el camino que usa Postgres para carga
    masiva: no evalúa conflictos, no hace 533 round-trips y escribe con mucho
    menos WAL. Sirve porque la tabla destino está recién creada y vacía.
    """
    total = len(rows)
    t0 = time.time()

    def progreso(n):
        elapsed = time.time() - t0
        rate = n / elapsed if elapsed else 0
        eta = (total - n) / rate if rate else 0
        print(f"   [{SCHEMA}.{table}] {n:>7,}/{total:,} filas  "
              f"{rate:>6.0f} filas/s  ETA {eta/60:>4.1f} min", flush=True)

    src = _RowsAsCopyText(rows, cols, on_progress=progreso)
    col_list = ", ".join(f'"{c}"' for c in cols)
    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {SCHEMA}.{table} ({col_list}) FROM STDIN",
            src, size=1 << 20,
        )
    conn.commit()
    print(f"   ✅ {SCHEMA}.{table}: {total:,} filas en {(time.time()-t0)/60:.1f} min "
          f"({total/max(time.time()-t0, 1):.0f} filas/s)")


def push(conn, table: str, cols: list[str], rows: list[dict], pk: str,
         batch: int, dry_run: bool):
    """Sube `rows` en batches con upsert, para cuando la tabla ya tiene datos."""
    total = len(rows)
    if total == 0:
        print(f"   (nada que subir a {SCHEMA}.{table})")
        return

    n_batches = (total + batch - 1) // batch
    col_list = ", ".join(f'"{c}"' for c in cols)
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != pk)
    sql = (
        f'INSERT INTO {SCHEMA}.{table} ({col_list}) VALUES %s '
        f'ON CONFLICT ("{pk}") DO UPDATE SET {updates}'
    )

    t0 = time.time()
    done = 0

    for i in range(0, total, batch):
        chunk = rows[i : i + batch]
        idx = i // batch + 1

        if not dry_run:
            values = [tuple(r[c] for c in cols) for r in chunk]
            for attempt in range(1, 4):
                try:
                    with conn.cursor() as cur:
                        execute_values(cur, sql, values, page_size=batch)
                    conn.commit()
                    break
                except psycopg2.Error as e:
                    conn.rollback()
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


# ── SCHEMA ───────────────────────────────────────────────────────────────────
def init_schema(conn):
    """Aplica supabase_schema.sql. Esto VACÍA las tablas (hace DROP)."""
    if not SCHEMA_SQL.exists():
        print(f"❌ {SCHEMA_SQL} no existe.")
        sys.exit(1)
    print(f"\n🏗️  Aplicando {SCHEMA_SQL.name}...")
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name", (SCHEMA,))
        objs = cur.fetchall()
    print(f"   ✅ schema {SCHEMA}: " + ", ".join(f"{n}" for n, _ in objs))


# Vistas materializadas de agregados. Se recalculan solo aquí: sus datos no
# cambian entre cargas, y dejarlas como vistas normales metía hasta 3 s en cada
# pantalla de reportes de la app.
MATVIEWS = [
    "mv_priority_counts", "mv_state_counts",
    "mv_top_parties", "mv_secured_parties", "mv_scraper_status",
]


def load_swap(conn, table: str, cols: list[str], rows: list[dict]) -> None:
    """
    Carga a una tabla de staging y traslada el contenido a la definitiva al
    final, sin que la webapp vea nunca una tabla vacía.

    El problema que resuelve: recrear la tabla y cargarla directo la dejaba en 0
    filas durante toda la carga — la app estuvo sin datos ~15 minutos.

    POR QUÉ NO SE RENOMBRAN LAS TABLAS
    ----------------------------------
    La solución obvia sería cargar en `maestro__nuevo` y hacer
    `RENAME maestro → viejo; RENAME nuevo → maestro`. No funciona: en Postgres
    las vistas se enlazan al OID de la tabla, no a su nombre. Tras el rename,
    maestro_live seguiría leyendo la tabla vieja, y el DROP de esa tabla se
    llevaría por delante todas las vistas (maestro_live, las mv_* y las ocho de
    public) con CASCADE.

    Lo que se hace en cambio: la parte lenta —traer 532.167 filas por la red—
    va a la tabla de staging, fuera del camino de nadie. Después, en UNA
    transacción, se vacía la definitiva y se copia desde staging con un
    INSERT ... SELECT, que es local al servidor y tarda segundos. La tabla
    nunca cambia de identidad, así que las vistas no se enteran, y los lectores
    solo esperan durante esa transacción corta en vez de ver datos vacíos.
    """
    tmp = f"{table}__staging"
    col_list = ", ".join(f'"{c}"' for c in cols)

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{tmp}")
        # Sin índices: cargar sin ellos es más rápido y en staging no se consulta.
        cur.execute(f"CREATE UNLOGGED TABLE {SCHEMA}.{tmp} "
                    f"(LIKE {SCHEMA}.{table})")
    conn.commit()

    copy_into(conn, tmp, cols, rows)

    print(f"   trasladando a {SCHEMA}.{table} (transacción corta)...")
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {SCHEMA}.{table}")
        cur.execute(f"INSERT INTO {SCHEMA}.{table} ({col_list}) "
                    f"SELECT {col_list} FROM {SCHEMA}.{tmp}")
        movidas = cur.rowcount
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE {SCHEMA}.{tmp}")
    conn.commit()
    print(f"   ✅ {SCHEMA}.{table}: {movidas:,} filas trasladadas en "
          f"{time.time()-t0:.0f}s (la app no vio la tabla vacía)")


def finalize(conn, tables: list[str]):
    """ANALYZE + REFRESH tras cargar. Sin esto el planner sigue creyendo que
    las tablas están vacías y elige seq scans donde hay índice."""
    print("\n🔧 Actualizando estadísticas y agregados...")
    prev_autocommit = conn.autocommit
    conn.commit()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for t in tables:
                t0 = time.time()
                cur.execute(f"ANALYZE {SCHEMA}.{t}")
                print(f"   ANALYZE {SCHEMA}.{t:<14} {time.time()-t0:>5.1f}s")
            for mv in MATVIEWS:
                t0 = time.time()
                cur.execute(f"REFRESH MATERIALIZED VIEW {SCHEMA}.{mv}")
                print(f"   REFRESH {SCHEMA}.{mv:<14} {time.time()-t0:>5.1f}s")
    finally:
        conn.autocommit = prev_autocommit


# ── EXTRACCIÓN DESDE movik.db ────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    if not DB_FILE.exists():
        print(f"❌ {DB_FILE} no existe.")
        sys.exit(1)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


from census_schema import CARRIER_COLS, INT_COLS as INT_CARRIER_COLS


def extract_carriers(conn) -> list[dict]:
    print("\n📥 Leyendo carriers de movik.db...")
    cols_sql = ", ".join(f'"{c}"' for c in CARRIER_COLS)
    rows = []
    for r in conn.execute(f"SELECT {cols_sql} FROM carriers"):
        d = {c: (to_int(r[c]) if c in INT_CARRIER_COLS else r[c]) for c in CARRIER_COLS}
        d["dot_number"] = str(d["dot_number"])
        rows.append(d)
    print(f"   {len(rows):,} carriers ({len(CARRIER_COLS)} columnas)")
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

# El maestro lleva TODAS las columnas del censo más lo que sale del cruce con
# UCC. Se arma desde CARRIER_COLS en vez de listarlas: con 145 nombres, una
# lista escrita a mano se desalinea a la primera.
MAESTRO_SELECT = f"""
SELECT
  {', '.join(f'c."{c}"' for c in CARRIER_COLS)},
  s.status     AS log_status,
  s.error_msg  AS log_error,
  s.scraped_at AS log_scraped_at,
  r.rep_id, r.n_filings, r.n_filed, r.n_lapsed,
  f.alert_priority, f.ucc_number, f.date_filed, f.expires_date, f.days_to_expiry,
  f.secured_party, f.secured_party_type, f.filing_status, f.state_registry
FROM carriers c
LEFT JOIN scrape_log s  ON s.dot_number = c.dot_number
LEFT JOIN mv_rep     r  ON r.dot_number = c.dot_number
LEFT JOIN ucc_filings f ON f.id = r.rep_id
"""

# Columnas que NO vienen del censo: las calcula este script al cruzar.
MAESTRO_EXTRA = [
    "units", "priority", "prio_rank", "ucc_found", "ucc_number", "date_filed",
    "expires_date", "expires_date_iso", "days_to_expiry", "secured_party",
    "secured_party_type", "filing_status", "state_registry", "n_filings",
    "scraped_at", "notes", "search_name",
]
MAESTRO_COLS = CARRIER_COLS + MAESTRO_EXTRA


def build_maestro(conn) -> list[dict]:
    print("\n📥 Construyendo el maestro (carriers × scrape_log × UCC)...")
    conn.executescript(MAESTRO_SQL)

    rows = []
    for r in conn.execute(MAESTRO_SELECT):
        state = (r["phy_state"] or "").strip()
        status = r["log_status"]

        # Misma regla que db.ts y 03_build_master.py: sin fila en scrape_log el
        # carrier NUNCA se consultó → "Sin datos", no P1. Marcarlo P1 lo
        # volvería un lead falso indistinguible de uno real.
        if status == "error":
            priority = "Error"
        elif status == "not_found":
            priority = "P1"
        elif status == "found":
            # 'found' sin filing representativo es un dato roto, no un P1.
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

        # Las 145 del censo se copian tal cual; solo se normalizan las que el
        # esquema declara enteras.
        fila = {c: (to_int(r[c]) if c in INT_CARRIER_COLS else r[c])
                for c in CARRIER_COLS}
        fila["dot_number"] = str(r["dot_number"])
        fila["phy_state"] = state

        rows.append({
            **fila,
            "units": power if power else (truck if truck else 0),
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
    ap.add_argument("--init-schema", action="store_true",
                    help="Aplicar supabase_schema.sql antes de cargar (VACÍA las tablas)")
    ap.add_argument("--schema-only", action="store_true",
                    help="Aplicar supabase_schema.sql y salir")
    ap.add_argument("--upsert", action="store_true",
                    help="Método viejo: upsert fila por fila en batches. Más "
                         "lento y pesado para el servidor; solo para cargas "
                         "parciales donde haya que preservar lo que ya está")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extrae y arma todo pero no escribe en Supabase")
    args = ap.parse_args()

    targets = args.only or ["carriers", "ucc_filings", "maestro"]

    print(f"\n🚀 Movik → Supabase  ·  schema \"{SCHEMA}\"")
    print(f"   Origen:  {DB_FILE}")
    if args.dry_run:
        print("   ⚠️  DRY RUN: no se escribe nada")

    pg = None if args.dry_run else connect_pg()

    if (args.init_schema or args.schema_only) and not args.dry_run:
        init_schema(pg)
        if args.schema_only:
            pg.close()
            print("\n✅ Schema aplicado.")
            return

    print(f"   Tablas:  {', '.join(targets)}")
    print(f"   Método:  " + ("upsert por batches de "
                             f"{args.batch:,}" if args.upsert
                             else "COPY + staging con traslado atómico"))

    conn = open_db()
    t0 = time.time()

    def cargar(table, cols, rows, pk):
        if args.dry_run:
            print(f"   (dry run: {len(rows):,} filas para {SCHEMA}.{table})")
        elif args.upsert:
            push(pg, table, cols, rows, pk, args.batch, args.dry_run)
        else:
            load_swap(pg, table, cols, rows)

    try:
        if "carriers" in targets:
            cargar("carriers", CARRIER_COLS, extract_carriers(conn), "dot_number")

        if "ucc_filings" in targets:
            cargar("ucc_filings", UCC_COLS, extract_ucc(conn), "id")

        if "maestro" in targets:
            cargar("maestro", MAESTRO_COLS, build_maestro(conn), "dot_number")

        if not args.dry_run:
            finalize(pg, targets)
    finally:
        conn.close()
        if pg:
            pg.close()

    print(f"\n✅ Listo en {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
