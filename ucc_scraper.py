"""
movik/ucc_scraper.py
====================
Enriquecimiento UCC para carriers de Florida.

Fuente:
  FL → https://publicsearchapi.floridaucc.com/search   (backend JSON de floridaucc.com)

  NOTA IMPORTANTE — por qué API y no scraping de HTML:
  floridaucc.com es una SPA de React. Pedir /search por HTTP devuelve un shell
  de ~1.3 KB con <div id="root"> y "You need to enable JavaScript to run this
  app". Los resultados NUNCA están en el HTML, así que un parser de BeautifulSoup
  contra esa página siempre daría match_found=False. El backend real es
  publicsearchapi.floridaucc.com, que devuelve JSON limpio.

  FLUJO DE DOS PASOS (verificado en el bundle main.b002c695.js):
    PASO A — Búsqueda:
      GET /search?searchOptionType=OrganizationDebtorName
                 &searchOptionSubOption=FiledAndLapsedCompactDebtorNameList
                 &searchCategory=Exact&text={NOMBRE}
      → debtors[]: {uccNumber, name, address, city, zipCode, state, status}
        (status = Filed|Lapsed). NO trae fechas ni secured party.

    PASO B — Detalle (por cada uccNumber del paso A):
      GET /filing-details?searchOptionType=DocumentNumber&filingNumber={UCC}
      → {fileDate, expirationDate, status, documentType,
         debtors[], secureds[{name,address,city,state,zipCode}]}
      La SPA lo llama con re.b.get(oe.d1,"/filing-details",{queryStringParameters})
      donde oe.d1 == publicsearchapi.floridaucc.com. Confirmado con
      PFKR CARRIER LLC / UCC 202203735800:
        fileDate=2022-11-28  expirationDate=2027-11-28
        secured=FIRST CORPORATE SOLUTIONS, AS REPRESENTATIVE (Sacramento, CA).

  Con el paso B tenemos expires_date real, así que days_to_expiry se calcula y
  las prioridades por fecha (P2b/P2c/P3/P4) ya son posibles. Ver
  ScrapeResult.alert_priority.

  CA → bizfileonline.sos.ca.gov  (NO implementado: es otra app con otra API)
  TX → NO implementado

Uso:
  python ucc_scraper.py --states FL          # scrape de Florida
  python ucc_scraper.py --state FL --test 5  # prueba con 5 carriers
  python ucc_scraper.py --reset              # borra checkpoint y reinicia
  python ucc_scraper.py --debug VBZ GROUP CORP   # debug de un nombre

Instalar:
  pip install httpx[http2] duckdb rich
"""

import asyncio
import httpx
import sqlite3
import duckdb

from census_schema import CARRIER_COLS, sql_type
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from dataclasses import dataclass, field
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from rich.console import Console
    RICH = True
except ImportError:
    RICH = False
    print("TIP: pip install rich  para mejor output")

# ────────────────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────────────────
CSV_FILE     = "census_file_full.csv"
DB_FILE      = "movik.db"
CHECKPOINT   = "scraper_checkpoint.json"

STATES       = ["FL"]                # CA necesita su propio scraper (bizfileonline)
CONCURRENT   = 3                     # workers simultáneos
RATE_PER_SEC = 3.0                   # techo global de requests/segundo
TIMEOUT      = 25                    # segundos por request
MAX_RETRIES  = 4
BATCH_SIZE   = 200                   # guardar checkpoint cada N carriers

# Corte de seguridad: si el registro empieza a rechazarnos de forma sostenida,
# paramos en vez de seguir machacando (y en vez de generar datos basura).
ABORT_AFTER_CONSECUTIVE_429 = 25

FL_API = "https://publicsearchapi.floridaucc.com/search"
FL_PARAMS = {
    "searchOptionType": "OrganizationDebtorName",
    # Filed AND Lapsed: necesitamos los vencidos para distinguir P2a de P4.
    "searchOptionSubOption": "FiledAndLapsedCompactDebtorNameList",
    "searchCategory": "Exact",
}

# PASO B — detalle por UCC number. La forma DocumentNumber solo necesita el
# número de filing (no el rowNumber), así que basta con el uccNumber del paso A.
FL_DETAIL_API = "https://publicsearchapi.floridaucc.com/filing-details"
FL_DETAIL_PARAMS = {"searchOptionType": "DocumentNumber"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://floridaucc.com",
    "Referer": "https://floridaucc.com/",
    "Connection": "keep-alive",
}

console = Console() if RICH else None


class RateLimiter:
    """Techo global de req/s compartido por todos los workers."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self):
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            delay = self._next - now
            self._next += self._interval
        if delay > 0:
            await asyncio.sleep(delay)


class BanDetector:
    """Cuenta 429s consecutivos para abortar si nos están rechazando."""

    def __init__(self, limit: int):
        self.limit = limit
        self.consecutive = 0
        self.tripped = False

    def hit_429(self):
        self.consecutive += 1
        if self.consecutive >= self.limit:
            self.tripped = True

    def ok(self):
        self.consecutive = 0


# ────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class UCCFiling:
    ucc_number:       str
    filing_status:    str
    date_filed:       str = ""
    expires_date:     str = ""
    secured_party:    str = ""
    secured_party_addr: str = ""
    filing_type:      str = "UCC1"
    days_to_expiry:   Optional[int] = None

    def __post_init__(self):
        self._recompute_days()

    def _recompute_days(self):
        """
        days_to_expiry queda NULL si no hay expires_date. El paso B (detalle) la
        llena con la expirationDate real; entonces este cálculo la convierte a
        días hasta el vencimiento (negativo = ya venció).
        """
        self.days_to_expiry = None
        if not self.expires_date:
            return
        for fmt in ("%m/%d/%Y", "%Y%m%d", "%m-%d-%Y", "%Y-%m-%d"):
            try:
                exp = datetime.strptime(self.expires_date, fmt).date()
                self.days_to_expiry = (exp - date.today()).days
                return
            except ValueError:
                continue

    def apply_detail(self, date_filed: str, expires_date: str,
                     secured_party: str, secured_party_addr: str,
                     filing_type: str = "", status: str = ""):
        """Enriquece el filing con los datos del paso B y recalcula los días."""
        self.date_filed = date_filed or self.date_filed
        self.expires_date = expires_date or self.expires_date
        self.secured_party = secured_party or self.secured_party
        if secured_party_addr:
            self.secured_party_addr = secured_party_addr
        if filing_type:
            self.filing_type = filing_type
        if status:
            self.filing_status = status
        self._recompute_days()


@dataclass
class ScrapeResult:
    dot_number:  str
    legal_name:  str
    state:       str
    match_found: bool
    filings:     list = field(default_factory=list)
    error:       Optional[str] = None
    scraped_at:  str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def alert_priority(self) -> str:
        """
        Modelo de prioridades, con las fechas reales del paso B (detalle):

          ERROR = la consulta falló. NUNCA P1: un fallo de red no es
                  evidencia de que el carrier no tenga UCC.
          P1    = sin UCC registrado
          P2a   = todos los filings Lapsed (venció; sin fecha exacta útil)
          P2b   = Filed + expires_date <= 30 días
          P2c   = Filed + expires_date 31-60 días
          P3    = Filed + expires_date 61-365 días
          P4    = Filed + expires_date > 365 días (o Filed sin fecha)

        Un solo filing activo (Filed) basta para que el carrier tenga gravamen
        vigente, así que P2a exige que TODOS estén vencidos. Cuando hay filings
        Filed, la urgencia la marca el que vence antes.
        """
        if self.error:
            return "ERROR"
        if not self.match_found or not self.filings:
            return "P1"

        filed = [f for f in self.filings
                 if (f.filing_status or "").strip().lower() == "filed"]

        # Ningún Filed → todos vencidos (Lapsed): sin fecha exacta útil.
        if not filed:
            return "P2a"

        # Hay al menos un Filed: la urgencia la marca el que vence antes.
        dated = [f.days_to_expiry for f in filed if f.days_to_expiry is not None]
        if not dated:
            # Filed pero sin fecha (el detalle falló): no podemos afinar → P4.
            return "P4"
        soonest = min(dated)
        if soonest <= 30:
            return "P2b"
        if soonest <= 60:
            return "P2c"
        if soonest <= 365:
            return "P3"
        return "P4"


# ────────────────────────────────────────────────────────────────────────────
# DATABASE — SQLite con WAL para escrituras rápidas
# ────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

/* La tabla carriers la crea _carriers_ddl() desde census_schema: son 145
   columnas y escribirlas aquí a mano garantizaba que se desincronizaran. */

CREATE TABLE IF NOT EXISTS ucc_filings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    dot_number           TEXT    NOT NULL,
    legal_name_searched  TEXT,
    match_found          INTEGER NOT NULL DEFAULT 0,
    alert_priority       TEXT,
    ucc_number           TEXT,
    date_filed           TEXT,
    expires_date         TEXT,
    days_to_expiry       INTEGER,
    secured_party        TEXT,
    secured_party_addr   TEXT,
    filing_type          TEXT,
    filing_status        TEXT,
    state_registry       TEXT,
    scraped_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dot        ON ucc_filings(dot_number);
CREATE INDEX IF NOT EXISTS idx_priority   ON ucc_filings(alert_priority);
CREATE INDEX IF NOT EXISTS idx_expires    ON ucc_filings(expires_date);
CREATE INDEX IF NOT EXISTS idx_state      ON ucc_filings(state_registry);

CREATE TABLE IF NOT EXISTS scrape_log (
    dot_number  TEXT PRIMARY KEY,
    scraped_at  TEXT NOT NULL,
    status      TEXT NOT NULL,   -- 'found' | 'not_found' | 'error'
    filings_n   INTEGER DEFAULT 0,
    priority    TEXT,
    error_msg   TEXT
);
"""

def _carriers_ddl() -> str:
    """CREATE TABLE de carriers armado desde census_schema, no a mano.

    Son 145 columnas: escribirlas aquí y mantenerlas sincronizadas con los otros
    scripts era pedir que alguna quedara desalineada.
    """
    defs = [f'    "{c}" {sql_type(c)}' + (" PRIMARY KEY" if c == "dot_number" else "")
            for c in CARRIER_COLS]
    return "CREATE TABLE IF NOT EXISTS carriers (\n" + ",\n".join(defs) + "\n);"


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.executescript(_carriers_ddl())
    # Tabla vieja de 20 columnas: se le agregan las que falten sin perder datos.
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(carriers)")}
    for c in CARRIER_COLS:
        if c not in existentes:
            conn.execute(f'ALTER TABLE carriers ADD COLUMN "{c}" {sql_type(c)}')
    conn.commit()
    return conn


def save_result(conn: sqlite3.Connection, r: ScrapeResult):
    status = "error" if r.error else ("found" if r.match_found else "not_found")

    conn.execute(
        "INSERT OR REPLACE INTO scrape_log VALUES (?,?,?,?,?,?)",
        (r.dot_number, r.scraped_at, status, len(r.filings), r.alert_priority, r.error)
    )

    # Un error NO se escribe en ucc_filings. Si lo hiciéramos como P1, un
    # timeout o un baneo quedaría indistinguible de "este carrier no tiene
    # UCC" y contaminaría la lista de leads con falsos P1.
    if r.error:
        conn.commit()
        return

    # Fuera lo que este carrier tuviera de una consulta anterior. ucc_filings no
    # tiene clave única —el id es AUTOINCREMENT— así que sin esto un re-scrape
    # (modo incremental) duplicaría cada filing en vez de actualizarlo, y el
    # maestro contaría dos veces el mismo gravamen. En el backfill cada DOT se
    # consulta una sola vez y este DELETE no borra nada.
    conn.execute("DELETE FROM ucc_filings WHERE dot_number = ?", (r.dot_number,))

    if r.match_found and r.filings:
        for f in r.filings:
            conn.execute("""
                INSERT INTO ucc_filings
                  (dot_number, legal_name_searched, match_found, alert_priority,
                   ucc_number, date_filed, expires_date, days_to_expiry,
                   secured_party, secured_party_addr, filing_type,
                   filing_status, state_registry, scraped_at)
                VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r.dot_number, r.legal_name, r.alert_priority,
                f.ucc_number, f.date_filed, f.expires_date, f.days_to_expiry,
                f.secured_party, f.secured_party_addr, f.filing_type,
                f.filing_status, r.state, r.scraped_at
            ))
    else:
        conn.execute("""
            INSERT OR IGNORE INTO ucc_filings
              (dot_number, legal_name_searched, match_found, alert_priority, state_registry, scraped_at)
            VALUES (?,?,0,'P1',?,?)
        """, (r.dot_number, r.legal_name, r.state, r.scraped_at))

    conn.commit()


# ────────────────────────────────────────────────────────────────────────────
# API CLIENT
# ────────────────────────────────────────────────────────────────────────────
class RateLimited(Exception):
    pass


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    limiter: RateLimiter,
    ban: BanDetector,
) -> Optional[dict]:
    """
    Devuelve el JSON decodificado, o None si la consulta falló de forma
    definitiva. Lanza RateLimited si el registro nos está rechazando.
    """
    for attempt in range(MAX_RETRIES):
        if ban.tripped:
            raise RateLimited("corte por 429 sostenido")
        await limiter.wait()
        try:
            r = await client.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)

            if r.status_code == 200:
                ban.ok()
                try:
                    return r.json()
                except json.JSONDecodeError:
                    return None

            if r.status_code == 429:
                ban.hit_429()
                await asyncio.sleep(10 * (attempt + 1))
                continue

            if r.status_code in (500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt)
                continue

            # 4xx que no sea 429: no reintentar, no es transitorio.
            return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.ReadError, httpx.PoolTimeout):
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    return None


def _filings_from_payload(payload: dict) -> list:
    """
    PASO A → UCCFilings (solo lo que trae la búsqueda: ucc + status).
    Las fechas y el secured party los llena el PASO B (detalle).

    Shape verificado (PFKR CARRIER LLC):
      {"debtors":[{"rowNumber":6472387,"name":"PFKR CARRIER LLC",
                   "uccNumber":"202203735800","address":..,"city":..,
                   "zipCode":..,"state":"FL","status":"Filed"}],
       "totalExactMatches":1}
    """
    debtors = (payload or {}).get("debtors") or []
    filings = []
    seen = set()
    for d in debtors:
        ucc = (d.get("uccNumber") or "").strip()
        if not ucc or ucc in seen:
            continue
        seen.add(ucc)
        filings.append(UCCFiling(
            ucc_number=ucc,
            filing_status=(d.get("status") or "").strip(),
            secured_party_addr=", ".join(
                x for x in [d.get("address"), d.get("city"),
                            d.get("state"), d.get("zipCode")] if x
            )[:300],
        ))
    return filings


def _iso_to_mdy(s: str) -> str:
    """'2022-11-28T05:00:00Z' → '11/28/2022'. Cadena vacía si no parsea."""
    if not s:
        return ""
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return ""


def _apply_detail_payload(f: UCCFiling, payload: dict):
    """
    PASO B → enriquece un UCCFiling con la respuesta de /filing-details.

    Shape verificado (UCC 202203735800):
      {"uccNumber":"202203735800","status":"Filed",
       "fileDate":"2022-11-28T05:00:00Z","expirationDate":"2027-11-28T05:00:00Z",
       "documentType":"UCC1",
       "secureds":[{"name":"FIRST CORPORATE SOLUTIONS, AS REPRESENTATIVE",
                    "address":"914 S STREET / SPRS@FICOSO.COM",
                    "city":"SACRAMENTO","state":"CA","zipCode":"95811"}], ...}
    """
    p = payload or {}
    secured_name = ""
    secured_addr = ""
    secureds = p.get("secureds") or []
    if secureds:
        sp = secureds[0]
        secured_name = (sp.get("name") or "").strip()
        # El registro a veces mete el email en el address tras " / ": lo cortamos.
        street = (sp.get("address") or "").split(" / ")[0].strip()
        secured_addr = ", ".join(
            x for x in [street, sp.get("city"), sp.get("state"), sp.get("zipCode")]
            if x
        )[:300]

    f.apply_detail(
        date_filed=_iso_to_mdy(p.get("fileDate") or ""),
        expires_date=_iso_to_mdy(p.get("expirationDate") or ""),
        secured_party=secured_name,
        secured_party_addr=secured_addr,
        filing_type=(p.get("documentType") or "").strip(),
        status=(p.get("status") or "").strip(),
    )


async def enrich_filing(
    client: httpx.AsyncClient,
    f: UCCFiling,
    limiter: RateLimiter,
    ban: BanDetector,
):
    """PASO B por filing: pide /filing-details y aplica fechas + secured party."""
    params = dict(FL_DETAIL_PARAMS, filingNumber=f.ucc_number)
    j = await fetch_json(client, FL_DETAIL_API, params, limiter, ban)
    # Si el detalle falla, dejamos el filing con lo del paso A (status) en vez de
    # perderlo: la prioridad caerá a P2a/P4 sin fecha, que sigue siendo útil.
    if j and not j.get("notOk"):
        _apply_detail_payload(f, j.get("payload") or {})


# ────────────────────────────────────────────────────────────────────────────
# SCRAPER PRINCIPAL por carrier
# ────────────────────────────────────────────────────────────────────────────
async def scrape_fl(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    carrier: dict,
    limiter: RateLimiter,
    ban: BanDetector,
) -> ScrapeResult:
    dot   = carrier["dot_number"]
    name  = (carrier["legal_name"] or "").strip().upper()
    state = carrier["phy_state"]

    async with sem:
        try:
            # PASO A — búsqueda.
            params = dict(FL_PARAMS, text=name)
            j = await fetch_json(client, FL_API, params, limiter, ban)

            if j is None:
                return ScrapeResult(dot, name, state, False, error="timeout_or_network")

            if j.get("notOk"):
                msg = (j.get("friendlyMessageSummary") or "api_error")[:200]
                return ScrapeResult(dot, name, state, False, error=msg)

            filings = _filings_from_payload(j.get("payload") or {})

            # PASO B — detalle por cada UCC (fechas + secured party).
            for f in filings:
                await enrich_filing(client, f, limiter, ban)

            return ScrapeResult(dot, name, state, bool(filings), filings)

        except RateLimited:
            raise
        except Exception as e:
            return ScrapeResult(dot, name, state, False, error=str(e)[:200])


# ────────────────────────────────────────────────────────────────────────────
# CARGA DE CARRIERS DESDE CSV
# ────────────────────────────────────────────────────────────────────────────
SELECT_COLS = """
    dot_number, legal_name, dba_name, phy_state, phy_city,
    phy_street, phy_zip, phone, cell_phone, email_address,
    company_officer_1, power_units, truck_units, fleetsize,
    total_drivers, classdef, carrier_operation, safety_rating,
    mcs150_date, status_code
"""

WHERE_ACTIVOS = """
    WHERE phy_state IN ({states})
      AND status_code = 'A'
      AND legal_name IS NOT NULL
      AND trim(legal_name) <> ''
    ORDER BY mcs150_date DESC NULLS LAST
"""


def load_carriers(csv_path: str, db_path: str, states: list) -> list[dict]:
    """
    Carriers activos de los estados pedidos.

    Fuente preferida: census_file_full.csv, que es el censo completo de FMCSA.
    Si no está, cae a la tabla `carriers` de movik.db — mismas columnas, ya
    filtradas, puestas ahí por 05_load_carriers_db.py.

    El fallback existe por GitHub Actions: el CSV pesa 1.7 GB, no cabe en el
    repo ni tiene sentido bajarlo en cada run, pero movik.db sí viaja como
    artifact entre runs y trae los mismos carriers.

    Sin límite a propósito: `--test N` se aplica DESPUÉS de descontar lo ya
    scrapeado (ver run()). Recortar aquí daría los N primeros por mcs150_date,
    que a mitad de un backfill están todos hechos y el run no haría nada.
    """
    st_list = ", ".join(f"'{s}'" for s in states)
    where = WHERE_ACTIVOS.format(states=st_list)

    if Path(csv_path).exists():
        q = f"""SELECT {SELECT_COLS}
                FROM read_csv_auto('{csv_path}', all_varchar=true,
                                   header=true, ignore_errors=true)
                {where}"""
        con = duckdb.connect()
        rows = con.execute(q).fetchall()
        cols = [d[0] for d in con.description]
        print(f"✅ {len(rows):,} carriers activos en {states}  (fuente: {csv_path})")
        return [dict(zip(cols, r)) for r in rows]

    if Path(db_path).exists():
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(f"SELECT {SELECT_COLS} FROM carriers {where}").fetchall()
        except sqlite3.OperationalError as e:
            print(f"❌ {db_path} no tiene una tabla `carriers` usable: {e}")
            sys.exit(1)
        finally:
            con.close()
        print(f"✅ {len(rows):,} carriers activos en {states}  (fuente: {db_path})")
        return [dict(r) for r in rows]

    print(f"❌ No se encuentra ni {csv_path} ni {db_path}.")
    print("   Corre 01_census_incremental.py, o deja un movik.db con la tabla carriers.")
    sys.exit(1)


# ────────────────────────────────────────────────────────────────────────────
# CHECKPOINT
# ────────────────────────────────────────────────────────────────────────────
def load_ck(path: str) -> set:
    p = Path(path)
    return set(json.loads(p.read_text())) if p.exists() else set()


def load_done(ck_path: str, db_path: str, retry_errors: bool = False,
              refresh_days: Optional[int] = None) -> set:
    """
    DOTs que no hay que volver a consultar: el checkpoint JSON UNIDO a lo que
    ya registró scrape_log.

    El JSON solo no alcanza. En GitHub Actions el primer run baja el release
    db-seed, que lleva movik.db pero no el checkpoint, así que `done` salía
    vacío: el scraper tomaba los primeros N carriers por mcs150_date —todos ya
    scrapeados— y los repetía, sin avanzar el backfill y duplicando filas en
    ucc_filings. scrape_log viaja dentro de movik.db, así que sobrevive.

    Además el JSON se queda corto incluso en local: lo escribe solo este
    scraper, mientras que scrape_log recoge también lo que hizo el de CA.

    Con --retry-errors los fallidos quedan fuera del conjunto para reintentarlos.

    Con refresh_days=N salen también los consultados hace más de N días: es el
    modo incremental. Un UCC no es un dato fijo —vence, se renueva, aparece uno
    nuevo— así que un carrier consultado hace seis meses ya no dice la verdad.
    El resto del pipeline no cambia: al salir de `done`, vuelven a la cola de
    pendientes como cualquier carrier sin consultar.
    """
    done = load_ck(ck_path)
    if not Path(db_path).exists():
        return done

    conn = sqlite3.connect(db_path)
    try:
        done |= {str(r[0]) for r in conn.execute("SELECT dot_number FROM scrape_log")}
        if retry_errors:
            done -= {str(r[0]) for r in conn.execute(
                "SELECT dot_number FROM scrape_log WHERE status = 'error'")}
        if refresh_days:
            corte = (datetime.now(timezone.utc)
                     - timedelta(days=refresh_days)).isoformat()
            caducados = {str(r[0]) for r in conn.execute(
                "SELECT dot_number FROM scrape_log WHERE scraped_at < ?", (corte,))}
            done -= caducados
            print(f"🔄 Incremental: {len(caducados):,} carriers consultados hace "
                  f"más de {refresh_days} días vuelven a la cola")
    except sqlite3.OperationalError as e:
        print(f"⚠️  No se pudo leer scrape_log de {db_path}: {e}")
    finally:
        conn.close()
    return done


def save_ck(path: str, done: set):
    Path(path).write_text(json.dumps(list(done)))


def purge_errors_from_checkpoint():
    """
    Saca del checkpoint los carriers marcados como 'error' para que el
    próximo run los reintente. Necesario para checkpoints escritos por
    versiones que sí guardaban los fallidos como hechos.
    """
    if not Path(CHECKPOINT).exists() or not Path(DB_FILE).exists():
        print("⚠️  Sin checkpoint o sin DB: nada que reintentar.")
        return

    conn = sqlite3.connect(DB_FILE)
    errored = {str(r[0]) for r in
               conn.execute("SELECT dot_number FROM scrape_log WHERE status='error'")}
    conn.close()

    done = load_ck(CHECKPOINT)
    before = len(done)
    done -= errored
    save_ck(CHECKPOINT, done)
    print(f"🔁 Reintentar errores: {before:,} → {len(done):,} en checkpoint "
          f"({len(errored):,} fallidos liberados)")


# ────────────────────────────────────────────────────────────────────────────
# ORQUESTADOR PRINCIPAL
# ────────────────────────────────────────────────────────────────────────────
async def run(carriers: list, conn: sqlite3.Connection, ck_path: str,
              limit: Optional[int] = None, done: Optional[set] = None):
    if done is None:
        done = load_ck(ck_path)
    todo = [c for c in carriers if c["dot_number"] not in done]
    pendientes = len(todo)

    # `--test N` recorta aquí, sobre lo que falta, no sobre el censo entero.
    if limit:
        todo = todo[:limit]

    print(f"\n📋 Total: {len(carriers):,} | Procesados: {len(done):,} | "
          f"Pendientes: {pendientes:,}")
    if limit:
        print(f"🔬 --test {limit}: se corren {len(todo):,} de esos {pendientes:,}")
    print(f"⚙️  {CONCURRENT} workers | techo {RATE_PER_SEC} req/s | "
          f"ETA ~{len(todo)/RATE_PER_SEC/3600:.1f}h\n")
    if not todo:
        print("Nada pendiente. Usa --reset para reiniciar.")
        return

    sem     = asyncio.Semaphore(CONCURRENT)
    limiter = RateLimiter(RATE_PER_SEC)
    ban     = BanDetector(ABORT_AFTER_CONSECUTIVE_429)
    stats   = {"P1": 0, "P2a": 0, "P2b": 0, "P2c": 0, "P3": 0, "P4": 0, "errors": 0}
    t0      = time.time()
    aborted = False

    limits = httpx.Limits(max_connections=CONCURRENT + 5,
                          max_keepalive_connections=CONCURRENT)

    async with httpx.AsyncClient(limits=limits, http2=True) as client:
        for batch_start in range(0, len(todo), BATCH_SIZE):
            batch = todo[batch_start: batch_start + BATCH_SIZE]
            results = await asyncio.gather(
                *[scrape_fl(client, sem, c, limiter, ban) for c in batch],
                return_exceptions=True
            )

            for i, res in enumerate(results):
                if isinstance(res, RateLimited):
                    aborted = True
                    continue
                if isinstance(res, BaseException):
                    res = ScrapeResult(
                        batch[i]["dot_number"],
                        batch[i].get("legal_name", ""),
                        batch[i].get("phy_state", ""),
                        False, error=str(res)[:200]
                    )

                save_result(conn, res)

                # Un carrier que falló NO entra al checkpoint: así el próximo
                # run lo reintenta en vez de dejarlo como Error para siempre.
                if res.error:
                    stats["errors"] += 1
                else:
                    done.add(res.dot_number)
                    stats[res.alert_priority] += 1

            save_ck(ck_path, done)

            processed = min(batch_start + len(batch), len(todo))
            elapsed   = time.time() - t0
            rate      = processed / max(elapsed, 1)
            remaining = (len(todo) - processed) / max(rate, 0.01)

            urgent = stats['P2a'] + stats['P2b'] + stats['P2c']
            print(
                f"[{processed:,}/{len(todo):,}] "
                f"🔴P1={stats['P1']:,} 🟠P2={urgent:,} "
                f"(a={stats['P2a']:,} b={stats['P2b']:,} c={stats['P2c']:,}) "
                f"🟡P3={stats['P3']:,} ⚪P4={stats['P4']:,} "
                f"❌err={stats['errors']:,} | "
                f"{rate:.1f}/s | ~{remaining/60:.0f}min restantes"
            )

            if aborted or ban.tripped:
                print(
                    f"\n🛑 ABORTADO: {ABORT_AFTER_CONSECUTIVE_429} respuestas 429 "
                    f"consecutivas — el registro nos está limitando.\n"
                    f"   Se paró para no seguir cargando el sitio y para no generar\n"
                    f"   P1 falsos. El checkpoint está guardado: relanza más tarde\n"
                    f"   y continúa donde quedó."
                )
                break

    elapsed_total = time.time() - t0
    print(f"\n{'⚠️  Parcial' if aborted else '✅ Completado'} en {elapsed_total/60:.1f} min")
    print(f"   P1(sin UCC)={stats['P1']:,} | P2a(lapsed)={stats['P2a']:,} | "
          f"P2b(<=30d)={stats['P2b']:,} | P2c(31-60d)={stats['P2c']:,} | "
          f"P3(61-365d)={stats['P3']:,} | P4(>365d)={stats['P4']:,} | "
          f"Errores={stats['errors']:,}")


# ────────────────────────────────────────────────────────────────────────────
# DEBUG: probar un carrier específico
# ────────────────────────────────────────────────────────────────────────────
async def debug_single(name: str):
    """Corre la consulta para un solo nombre e imprime la respuesta cruda."""
    params = dict(FL_PARAMS, text=name.upper())
    print(f"\n🔍 GET {FL_API}")
    print(f"   params: {params}\n")
    limiter = RateLimiter(RATE_PER_SEC)
    ban     = BanDetector(ABORT_AFTER_CONSECUTIVE_429)
    async with httpx.AsyncClient(http2=True) as client:
        # PASO A
        j = await fetch_json(client, FL_API, params, limiter, ban)
        if j is None:
            print("❌ Sin respuesta del API")
            return
        print("── PASO A: búsqueda (JSON crudo) ───────────────")
        print(json.dumps(j, indent=2)[:2000])
        filings = _filings_from_payload(j.get("payload") or {})

        # PASO B — detalle por cada UCC.
        for f in filings:
            dp = dict(FL_DETAIL_PARAMS, filingNumber=f.ucc_number)
            print(f"\n── PASO B: GET {FL_DETAIL_API}  {dp} ──")
            jd = await fetch_json(client, FL_DETAIL_API, dp, limiter, ban)
            if jd:
                print(json.dumps(jd, indent=2)[:2000])
                if not jd.get("notOk"):
                    _apply_detail_payload(f, jd.get("payload") or {})

        r = ScrapeResult("DEBUG", name.upper(), "FL", bool(filings), filings)
        print("\n── Parseado ────────────────────────────────────")
        print(f"ScrapeResult(match_found={r.match_found}, "
              f"alert_priority={r.alert_priority}, filings={len(filings)})")
        for f in filings:
            print(f"   • {f.ucc_number}  status={f.filing_status!r}  "
                  f"filed={f.date_filed!r}  expires={f.expires_date!r}  "
                  f"days_to_expiry={f.days_to_expiry}\n"
                  f"     secured_party={f.secured_party!r}\n"
                  f"     secured_party_addr={f.secured_party_addr!r}")


# ────────────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Movik UCC Scraper — FL")
    parser.add_argument("--test",  type=int, metavar="N", help="Testear con N carriers")
    parser.add_argument("--state", type=str, metavar="ST", help="Un estado (FL)")
    parser.add_argument("--states", nargs="+", metavar="ST", help="Varios estados (FL)")
    parser.add_argument("--reset", action="store_true", help="Borrar checkpoint")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Reintentar solo los carriers que fallaron")
    parser.add_argument("--refresh-older-than", type=int, metavar="D",
                        help="Modo incremental: reconsultar los carriers cuyo "
                             "último scrape tenga más de D días")
    parser.add_argument("--debug", nargs="+", metavar="NAME", help="Debug de un nombre")
    args = parser.parse_args()

    if args.debug:
        asyncio.run(debug_single(" ".join(args.debug)))
        return

    if args.reset and Path(CHECKPOINT).exists():
        Path(CHECKPOINT).unlink()
        print("🔄 Checkpoint borrado.")

    if args.retry_errors:
        purge_errors_from_checkpoint()

    if args.states:
        states = [s.upper() for s in args.states]
    elif args.state:
        states = [args.state.upper()]
    else:
        states = STATES

    unsupported = [s for s in states if s != "FL"]
    if unsupported:
        print(f"❌ Sin scraper para {unsupported}. Solo FL está implementado.")
        print("   CA usa bizfileonline.sos.ca.gov (otra API); TX no está hecho.")
        print("   Correrlos contra el registro de Florida daría datos sin sentido.")
        sys.exit(1)

    carriers = load_carriers(CSV_FILE, DB_FILE, states)
    if not carriers:
        print("No hay carriers. Verifica el CSV y los estados.")
        sys.exit(1)

    conn = init_db(DB_FILE)
    # Columnas explícitas, no VALUES posicional: el scraper solo trae las 20
    # que necesita para consultar el registro, y la tabla tiene 145. Con
    # posicional, los valores caerían en las columnas equivocadas.
    cols = [c for c in CARRIER_COLS if c in carriers[0]]
    placeholders = ",".join("?" * len(cols))
    conn.executemany(
        f'INSERT OR IGNORE INTO carriers ({", ".join(chr(34)+c+chr(34) for c in cols)}) '
        f"VALUES ({placeholders})",
        [tuple(c.get(k) for k in cols) for c in carriers]
    )
    conn.commit()
    print(f"💾 {DB_FILE} inicializado con {len(carriers):,} carriers")

    done = load_done(CHECKPOINT, DB_FILE, retry_errors=args.retry_errors,
                     refresh_days=args.refresh_older_than)
    asyncio.run(run(carriers, conn, CHECKPOINT, limit=args.test, done=done))


if __name__ == "__main__":
    main()
