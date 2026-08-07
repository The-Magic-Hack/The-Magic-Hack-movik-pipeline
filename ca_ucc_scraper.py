"""
movik/ca_ucc_scraper.py
=======================
Enriquecimiento UCC para carriers de California.

NO toca ucc_scraper.py (Florida). Comparte movik.db y la tabla ucc_filings,
distinguiendo por state_registry = 'CA'.

Fuente:
  CA -> https://bizfileonline.sos.ca.gov/api/Records/uccsearch

  ENDPOINT (verificado en el bundle static/js/main.ff89e940.js):
    La SPA define SEARCH(payload, category) ->
        POST /api/Records/{category}search   con body JSON
    y la categoria del portal UCC es "ucc" (bloque de config:
        {label:"UCC", component:"UCC", parameter:"ucc", sourceTypeId:1999,
         searchHasPagination:true, advancedSearchParameters:[STATUS,
         RECORD_TYPE_ID, FILING_DATE, LAPSE_DATE]}).

  BODY REQUERIDO — las cuatro claves del bloque advancedSearchParameters son
  obligatorias. Mandar solo SEARCH_VALUE devuelve 400 con
  internalerror="The given key was not present in the dictionary":
    {"SEARCH_VALUE": <nombre>,
     "STATUS": "ALL",                 # ALL | ACTIVE_UNLAPSED | ACTIVE_LAPSED_UNLAPSED
     "RECORD_TYPE_ID": "0",           # 0=All, 2170=Financing Statement, 2154=Judgment
                                      # Lien, 2175=State Tax Lien, 2174=Federal Tax
                                      # Lien, 2155=Attachment
     "FILING_DATE": {"start": null, "end": null},
     "LAPSE_DATE":  {"start": null, "end": null}}

  STATUS="ALL" es lo que necesitamos: trae Active y Lapsed en la misma
  respuesta, que es lo que distingue P2a de P4.

  UNA SOLA LLAMADA — no hay paso B. A diferencia de Florida, la fila de
  resultados ya trae todo lo que guardamos:
    {"rows": {"<ID>": {"TITLE": ["DEBTOR - CIUDAD, ST"],
                       "SEC_PARTY": ["ACREEDOR - CIUDAD, ST"],
                       "RECORD_NUM": "U250200229522",
                       "FILING_DATE": "10/09/2025",
                       "LAPSE_DATE": "10/09/2030",
                       "STATUS": "Active",
                       "RECORD_TYPE": "UCC"}},
     "edge": {"offset": 0, "limit": 100, "total": 1}}

  Validado contra dos casos reales:
    DILPREET TRANSPORT INC -> U250200229522 / VALERE 225 TRUST /
                              Active  / 10/09/2025 / 10/09/2030
    TSI EXPRESS INC        ->  197699061462 / SALVEO 119 TRUST /
                              Lapsed  / 02/26/2019 / 02/26/2024

  OJO con RECORD_NUM: el portal es inconsistente con el prefijo "U". El filing
  de DILPREET viene "U250200229522" y el de TSI viene "197699061462" aunque la
  UI lo muestre como "U197699061462". Guardamos ucc_number tal cual lo devuelve
  el API y ademas ucc_number_norm no existe: si necesitas cruzar por numero,
  compara ignorando una "U" inicial.

  SIN TOKEN: el endpoint no pide auth ni CSRF. Basta con headers de navegador
  (Origin/Referer del propio dominio). Igual cebamos la sesion con un GET a
  /search/ucc para recoger las cookies de Incapsula, que es lo que hace el
  navegador real y reduce el riesgo de que el WAF nos corte.

  DIRECCIONES: el panel derecho de la UI trae la calle completa del debtor y
  del secured party via POST /api/FilingDetail/{id}. No lo llamamos: duplicaria
  el trafico y la ciudad+estado que ya trae SEC_PARTY es suficiente para
  calificar el lead. secured_party_addr = la parte "CIUDAD, ST".

Uso:
  python ca_ucc_scraper.py --test 10        # prueba con 10 carriers
  python ca_ucc_scraper.py                  # run completo (325K, ~45 h a 2 req/s)
  python ca_ucc_scraper.py --retry-errors   # reintenta los status='error' de CA
  python ca_ucc_scraper.py --alerts-only    # solo P2b/P2c conocidos + carriers nuevos
  python ca_ucc_scraper.py --reset          # borra checkpoint y reinicia

Instalar:
  pip install httpx openpyxl
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ────────────────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
XLSX_FILE    = BASE_DIR / "movik_carriers.xlsx"
XLSX_SHEET   = "CA"
CARRIER_CACHE = BASE_DIR / "ca_carriers_cache.csv"
DB_FILE      = BASE_DIR / "movik.db"
CHECKPOINT   = BASE_DIR / "ca_scraper_checkpoint.json"

STATE        = "CA"
# Defaults conservadores tras el bloqueo de Incapsula del 24/07/2026: la
# primera corrida a 2 req/s / 20 workers duro 2,337 carriers (~2,600 requests)
# antes de que el WAF empezara a devolver 403 con challenge JS. Ajustables con
# --rate y --workers.
CONCURRENT   = 5                     # workers simultaneos
RATE_PER_SEC = 0.4                   # techo global de requests/segundo
TIMEOUT      = 30                    # segundos por request
MAX_RETRIES  = 4
BATCH_SIZE   = 50                    # guardar checkpoint cada N carriers
PAGE_LIMIT   = 100                   # el API pagina de 100 en 100

# Techo de paginas por carrier. La busqueda del portal es "contains keywords",
# asi que una razon social generica arrastra cientos de filings ajenos: por
# ejemplo "F S TRANSPORTATION INC" devuelve total=221 y ni uno solo hace match
# exacto. Sin este techo, un punado de nombres genericos se come el presupuesto
# de requests de toda la corrida. 10 paginas = 1000 filings revisados; que el
# match exacto este mas alla de eso es practicamente imposible, pero si pasa lo
# registramos en vez de truncar en silencio.
MAX_PAGES    = 10

# Corte de seguridad: si el registro empieza a rechazarnos de forma sostenida,
# paramos en vez de seguir machacando (y en vez de generar datos basura).
ABORT_AFTER_CONSECUTIVE_429 = 25

CA_SEARCH_URL = "https://bizfileonline.sos.ca.gov/api/Records/uccsearch"
CA_PAGE_URL   = "https://bizfileonline.sos.ca.gov/search/ucc"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://bizfileonline.sos.ca.gov",
    "Referer": CA_PAGE_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def log(msg: str):
    print(msg, flush=True)


# ────────────────────────────────────────────────────────────────────────────
# CLASIFICACION DEL SECURED PARTY
# ────────────────────────────────────────────────────────────────────────────
# Se evalua en orden: factor -> trust -> bank -> other. El orden importa porque
# las listas se solapan. 'AMERICA' es el keyword mas laxo de todos (matchearia
# "AMERICAN TRUST FUND"), asi que bank va despues de trust; y factor va primero
# porque "TRIUMPH BUSINESS CAPITAL" es un factor, no un banco.
_FACTOR_KW = [
    "RTS", "OTR", "RIVIERA", "TRIUMPH", "APEX", "ECAPITAL", "TAFS",
    "PORTER FREIGHT", "AXLE", "ALTLINE", "PARAGON",
    "FACTORING", "FACTOR", "RECEIVABLE", "FUNDING",
]
_TRUST_KW = [
    "TRUST", "FUND", "HOLDINGS", "INVESTMENT", "CAPITAL TRUST",
]
_BANK_KW = [
    "BANK", "SBA", "WELLS", "CHASE", "AMERICA", "CAPITAL ONE",
    "CITIBANK", "TD BANK", "BANCORP", "CREDIT UNION",
    # En CA la SBA casi nunca aparece como acronimo: los filings la nombran
    # "U.S. SMALL BUSINESS ADMINISTRATION", que \bSBA\b no captura. Sin esta
    # linea los prestamos SBA caian en 'other' (2 de los 10 del test).
    "SMALL BUSINESS ADMINISTRATION",
]


def _compile(words):
    # \b en los dos extremos para que 'RTS' no matchee dentro de "PARTS" ni
    # 'SBA' dentro de un nombre cualquiera.
    return [re.compile(r"\b" + re.escape(w) + r"\b") for w in words]


_FACTOR_RE = _compile(_FACTOR_KW)
_TRUST_RE = _compile(_TRUST_KW)
_BANK_RE = _compile(_BANK_KW)


def classify_secured_party(name: str) -> str:
    """factor | trust | bank | other — ver nota de orden arriba."""
    if not name:
        return "other"
    n = name.upper()
    for group, label in ((_FACTOR_RE, "factor"), (_TRUST_RE, "trust"), (_BANK_RE, "bank")):
        for rx in group:
            if rx.search(n):
                return label
    return "other"


# ────────────────────────────────────────────────────────────────────────────
# NORMALIZACION DE NOMBRES
# ────────────────────────────────────────────────────────────────────────────
_PUNCT_RE = re.compile(r"[^A-Z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def norm_name(s: str) -> str:
    """Mayusculas, sin puntuacion, espacios colapsados."""
    if not s:
        return ""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", s.upper())).strip()


def split_name_location(value: str) -> tuple:
    """
    'DILPREET TRANSPORT INC - RIALTO, CA' -> ('DILPREET TRANSPORT INC', 'RIALTO, CA')

    Partimos por el ULTIMO ' - ' porque hay razones sociales que llevan guion
    dentro (p.ej. 'A-1 TRUCKING - LOS ANGELES, CA').
    """
    if not value:
        return "", ""
    idx = value.rfind(" - ")
    if idx == -1:
        return value.strip(), ""
    return value[:idx].strip(), value[idx + 3:].strip()


def parse_mmddyyyy(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────────────
# MODELO
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class Filing:
    ucc_number: str = ""
    date_filed: str = ""
    expires_date: str = ""          # lapse date
    secured_party: str = ""
    secured_party_addr: str = ""
    secured_party_type: str = "other"
    filing_type: str = "UCC"
    filing_status: str = ""         # Active | Lapsed
    days_to_expiry: Optional[int] = None

    def __post_init__(self):
        exp = parse_mmddyyyy(self.expires_date)
        self.days_to_expiry = (exp - date.today()).days if exp else None

    @property
    def is_active(self) -> bool:
        return self.filing_status.strip().lower() == "active"


@dataclass
class ScrapeResult:
    dot_number: str
    legal_name: str
    state: str = STATE
    match_found: bool = False
    filings: list = field(default_factory=list)
    error: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def alert_priority(self) -> str:
        """
        ERROR = la consulta fallo. NUNCA P1: un timeout no es evidencia de que
                el carrier no tenga UCC, y un P1 falso contamina los leads.
        P1  = sin UCC registrado
        P2a = todos los filings Lapsed
        P2b = Active + lapse_date <= 30 dias
        P2c = Active + lapse_date 31-60 dias
        P3  = Active + lapse_date 61-365 dias
        P4  = Active + lapse_date > 365 dias (o Active sin fecha)

        Un solo filing Active basta para que el carrier tenga gravamen vigente,
        asi que P2a exige que TODOS esten Lapsed. Con varios Active, la urgencia
        la marca el que vence antes.
        """
        if self.error:
            return "ERROR"
        if not self.match_found or not self.filings:
            return "P1"

        active = [f for f in self.filings if f.is_active]
        if not active:
            return "P2a"

        dated = [f.days_to_expiry for f in active if f.days_to_expiry is not None]
        if not dated:
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
# DB
# ────────────────────────────────────────────────────────────────────────────
SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_dot      ON ucc_filings(dot_number);
CREATE INDEX IF NOT EXISTS idx_priority ON ucc_filings(alert_priority);
CREATE INDEX IF NOT EXISTS idx_expires  ON ucc_filings(expires_date);
CREATE INDEX IF NOT EXISTS idx_state    ON ucc_filings(state_registry);

CREATE TABLE IF NOT EXISTS scrape_log (
    dot_number  TEXT PRIMARY KEY,
    scraped_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    filings_n   INTEGER DEFAULT 0,
    priority    TEXT,
    error_msg   TEXT
);
"""


def init_db(path) -> sqlite3.Connection:
    # El scraper de Florida esta corriendo contra esta misma base. WAL permite
    # lector+escritor concurrentes, y busy_timeout hace que ESTE proceso espere
    # en vez de reventar si FL tiene el lock de escritura. Mantenemos las
    # transacciones cortas para no hacerle esperar a el (que abre la conexion
    # sin busy_timeout y si fallaria).
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)

    # secured_party_type es nuevo (no existe en el esquema de FL). ALTER TABLE
    # ADD COLUMN es instantaneo en SQLite y no reescribe la tabla, asi que no
    # interrumpe al proceso de Florida; sus INSERT nombran columnas explicitas
    # y siguen siendo validos.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ucc_filings)")}
    if "secured_party_type" not in cols:
        conn.execute("ALTER TABLE ucc_filings ADD COLUMN secured_party_type TEXT")
        log("[db] columna secured_party_type anadida a ucc_filings")

    conn.commit()
    return conn


def save_batch(conn: sqlite3.Connection, results: list):
    """
    Escribe un lote entero en UNA transaccion. Agrupar reduce el numero de
    ventanas en las que tenemos el lock de escritura, que es lo que le importa
    al scraper de Florida corriendo en paralelo.
    """
    if not results:
        return
    for r in results:
        status = "error" if r.error else ("found" if r.match_found else "not_found")
        conn.execute(
            "INSERT OR REPLACE INTO scrape_log VALUES (?,?,?,?,?,?)",
            (r.dot_number, r.scraped_at, status, len(r.filings),
             r.alert_priority, r.error),
        )

        # Un error NO se escribe en ucc_filings. Si lo hicieramos como P1, un
        # timeout quedaria indistinguible de "este carrier no tiene UCC".
        if r.error:
            continue

        # Reemplazamos los filings previos de este DOT en CA para que un
        # re-scrape no duplique filas. El WHERE acota a CA: no tocamos FL.
        conn.execute(
            "DELETE FROM ucc_filings WHERE dot_number = ? AND state_registry = ?",
            (r.dot_number, r.state),
        )

        if r.match_found and r.filings:
            for f in r.filings:
                conn.execute("""
                    INSERT INTO ucc_filings
                      (dot_number, legal_name_searched, match_found, alert_priority,
                       ucc_number, date_filed, expires_date, days_to_expiry,
                       secured_party, secured_party_addr, secured_party_type,
                       filing_type, filing_status, state_registry, scraped_at)
                    VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.dot_number, r.legal_name, r.alert_priority,
                    f.ucc_number, f.date_filed, f.expires_date, f.days_to_expiry,
                    f.secured_party, f.secured_party_addr, f.secured_party_type,
                    f.filing_type, f.filing_status, r.state, r.scraped_at,
                ))
        else:
            conn.execute("""
                INSERT INTO ucc_filings
                  (dot_number, legal_name_searched, match_found, alert_priority,
                   state_registry, scraped_at)
                VALUES (?,?,0,'P1',?,?)
            """, (r.dot_number, r.legal_name, r.state, r.scraped_at))

    conn.commit()


# ────────────────────────────────────────────────────────────────────────────
# CARRIERS
# ────────────────────────────────────────────────────────────────────────────
def load_carriers_from_db() -> list:
    """
    Lee (dot, legal_name) de la tabla carriers de movik.db.

    Es la fuente por defecto desde que CA corre en GitHub Actions: el xlsx pesa
    127 MB, esta en .gitignore y no existe en el runner, asi que load_carriers()
    reventaba en openpyxl antes de la primera request — y `continue-on-error`
    lo pintaba de verde, con lo que el run terminaba en success habiendo hecho
    cero carriers.

    movik.db ya viaja al runner (cifrado) y tiene el censo completo, asi que es
    la misma lista sin archivo extra que arrastrar.
    """
    conn = sqlite3.connect(str(DB_FILE))
    try:
        rows = [(str(d), n) for d, n in conn.execute(
            "SELECT dot_number, legal_name FROM carriers "
            "WHERE phy_state = ? AND legal_name IS NOT NULL AND legal_name <> ''",
            (STATE,))]
    finally:
        conn.close()
    log(f"[carriers] {len(rows):,} de {STATE} desde {DB_FILE.name}")
    return rows


def load_carriers() -> list:
    """
    Lista de carriers de CA. Por defecto sale de movik.db; el xlsx solo se usa
    si esta presente, que es el caso en local donde ya estaba cacheado.
    """
    if not XLSX_FILE.exists():
        return load_carriers_from_db()

    if CARRIER_CACHE.exists() and CARRIER_CACHE.stat().st_mtime >= XLSX_FILE.stat().st_mtime:
        with open(CARRIER_CACHE, newline="", encoding="utf-8") as fh:
            rows = [(r[0], r[1]) for r in csv.reader(fh)]
        log(f"[carriers] {len(rows):,} desde cache {CARRIER_CACHE.name}")
        return rows

    log(f"[carriers] leyendo hoja '{XLSX_SHEET}' de {XLSX_FILE.name} (127 MB, tarda)...")
    import openpyxl

    t0 = time.time()
    wb = openpyxl.load_workbook(str(XLSX_FILE), read_only=True, data_only=True)
    ws = wb[XLSX_SHEET]

    rows = []
    header = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            header = list(row)
            i_dot = header.index("DOT #")
            i_name = header.index("Legal Name")
            continue
        dot = row[i_dot]
        name = row[i_name]
        if dot is None or not name:
            continue
        rows.append((str(dot).strip(), str(name).strip()))
    wb.close()

    with open(CARRIER_CACHE, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)

    log(f"[carriers] {len(rows):,} leidos en {time.time()-t0:.1f}s -> cache {CARRIER_CACHE.name}")
    return rows


def select_pending(conn, carriers: list, args) -> list:
    """Decide que carriers procesa esta corrida."""
    done = {r[0] for r in conn.execute("SELECT dot_number FROM scrape_log")}

    if args.retry_errors:
        # Solo los que fallaron Y son de CA (scrape_log es compartido con FL).
        ca_dots = {d for d, _ in carriers}
        errs = {r[0] for r in conn.execute(
            "SELECT dot_number FROM scrape_log WHERE status='error'")}
        target = errs & ca_dots
        pending = [(d, n) for d, n in carriers if d in target]
        log(f"[select] --retry-errors: {len(pending):,} carriers CA con status='error'")

    elif args.alerts_only:
        # P2b/P2c ya conocidos (hay que refrescar su fecha) + carriers nunca vistos.
        watch = {r[0] for r in conn.execute(
            "SELECT DISTINCT dot_number FROM ucc_filings "
            "WHERE state_registry=? AND alert_priority IN ('P2b','P2c')", (STATE,))}
        pending = [(d, n) for d, n in carriers if d in watch or d not in done]
        log(f"[select] --alerts-only: {len(pending):,} "
            f"({len(watch):,} en P2b/P2c + nuevos)")

    else:
        pending = [(d, n) for d, n in carriers if d not in done]
        log(f"[select] {len(pending):,} pendientes de {len(carriers):,} "
            f"({len(done & {d for d, _ in carriers}):,} ya hechos)")

    if args.test:
        pending = pending[:args.test]
        log(f"[select] --test {args.test}: recortado a {len(pending)}")
    return pending


# ────────────────────────────────────────────────────────────────────────────
# RATE LIMIT / BAN
# ────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """Techo global de req/s compartido por todos los workers."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0
        # Contamos aqui y no en el worker: un carrier puede costar varias
        # requests (paginacion, reintentos), y el rate que importa es el que ve
        # el portal, no el de carriers procesados.
        self.requests = 0

    async def wait(self):
        self.requests += 1
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
    def __init__(self, limit: int, limiter: "RateLimiter" = None):
        self.limit = limit
        self.consecutive = 0
        self.tripped = False
        # Para medir el umbral del WAF: en que numero de request llego el
        # primer bloqueo, y cuantos bloqueos hubo en total. Si el umbral se
        # repite cerca del mismo numero de requests con un rate 5x menor,
        # entonces Incapsula cuenta VOLUMEN y no velocidad, y bajar el rate no
        # nos va a salvar nunca: haria falta rotar IP.
        self._limiter = limiter
        self.first_block_at_request = None
        self.total_blocks = 0

    def ok(self):
        self.consecutive = 0

    def hit_429(self):
        self.consecutive += 1
        self.total_blocks += 1
        if self.first_block_at_request is None:
            self.first_block_at_request = (
                self._limiter.requests if self._limiter else -1
            )
            log(f"[WAF] primer bloqueo en la request #{self.first_block_at_request}")
        if self.consecutive >= self.limit:
            self.tripped = True


class RateLimited(Exception):
    pass


# ────────────────────────────────────────────────────────────────────────────
# API
# ────────────────────────────────────────────────────────────────────────────
def build_body(name: str, offset: int = 0) -> dict:
    body = {
        "SEARCH_VALUE": name,
        "STATUS": "ALL",
        "RECORD_TYPE_ID": "0",
        "FILING_DATE": {"start": None, "end": None},
        "LAPSE_DATE": {"start": None, "end": None},
    }
    if offset:
        body["OFFSET"] = offset
    return body


async def post_search(client, body, limiter, ban) -> Optional[dict]:
    """JSON decodificado, o None si fallo de forma definitiva."""
    for attempt in range(MAX_RETRIES):
        if ban.tripped:
            raise RateLimited("corte por 429 sostenido")
        await limiter.wait()
        try:
            r = await client.post(CA_SEARCH_URL, json=body, timeout=TIMEOUT)

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

            # 403 = el WAF (Incapsula) nos corto. Lo tratamos como el 429:
            # esperar y reintentar, y si es sostenido abortar.
            if r.status_code == 403:
                ban.hit_429()
                await asyncio.sleep(10 * (attempt + 1))
                continue

            return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.ReadError, httpx.PoolTimeout):
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return None
    return None


def parse_rows(payload: dict, searched: str) -> list:
    """
    Convierte rows{} en Filings, quedandose solo con los que hacen match EXACTO
    de razon social. La busqueda del portal es "contains (keywords)", asi que
    sin este filtro un carrier llamado 'TSI EXPRESS INC' se llevaria los filings
    de cualquier 'TSI EXPRESS LOGISTICS INC' y generaria leads falsos.
    """
    target = norm_name(searched)
    out = []
    for row in (payload.get("rows") or {}).values():
        titles = row.get("TITLE") or []
        if isinstance(titles, str):
            titles = [titles]

        matched = False
        for t in titles:
            debtor, _loc = split_name_location(t)
            if norm_name(debtor) == target:
                matched = True
                break
        if not matched:
            continue

        sec_raw = row.get("SEC_PARTY") or []
        if isinstance(sec_raw, str):
            sec_raw = [sec_raw]
        sec_name, sec_loc = split_name_location(sec_raw[0]) if sec_raw else ("", "")

        out.append(Filing(
            ucc_number=str(row.get("RECORD_NUM") or "").strip(),
            date_filed=str(row.get("FILING_DATE") or "").strip(),
            expires_date=str(row.get("LAPSE_DATE") or "").strip(),
            secured_party=sec_name,
            secured_party_addr=sec_loc,
            secured_party_type=classify_secured_party(sec_name),
            filing_type=str(row.get("RECORD_TYPE") or "UCC").strip(),
            filing_status=str(row.get("STATUS") or "").strip(),
        ))
    return out


async def scrape_carrier(client, dot, name, limiter, ban) -> ScrapeResult:
    res = ScrapeResult(dot_number=dot, legal_name=name)

    # El portal exige minimo 3 caracteres (minimumSearchCharacters en la config).
    if len(name.strip()) < 3:
        res.error = "nombre demasiado corto para el portal (min 3 chars)"
        return res

    try:
        payload = await post_search(client, build_body(name), limiter, ban)
    except RateLimited:
        raise
    if payload is None:
        res.error = "busqueda fallida tras reintentos"
        return res

    filings = parse_rows(payload, name)

    # Paginacion: el API devuelve de 100 en 100 y acepta OFFSET. Un carrier con
    # >100 filings es rarisimo, pero si lo truncamos silenciosamente el
    # days_to_expiry minimo saldria mal y con el la prioridad.
    edge = payload.get("edge") or {}
    total = int(edge.get("total") or 0)
    offset = PAGE_LIMIT
    while offset < total:
        if offset >= PAGE_LIMIT * MAX_PAGES:
            log(f"[trunc] {dot} {name}: {total} filings, cortado en "
                f"{PAGE_LIMIT * MAX_PAGES}")
            break
        try:
            page = await post_search(client, build_body(name, offset), limiter, ban)
        except RateLimited:
            raise
        if page is None:
            res.error = f"paginacion fallida en offset {offset}"
            return res
        filings.extend(parse_rows(page, name))
        offset += PAGE_LIMIT

    res.filings = filings
    res.match_found = bool(filings)
    return res


# ────────────────────────────────────────────────────────────────────────────
# CHECKPOINT
# ────────────────────────────────────────────────────────────────────────────
def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_checkpoint(stats: dict):
    # El reanudar se apoya en scrape_log (que es la verdad y ya esta en la
    # base), no en este archivo. Aqui solo guardamos progreso y contadores para
    # poder mirar como va la corrida sin abrir SQLite.
    CHECKPOINT.write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ────────────────────────────────────────────────────────────────────────────
# RUN
# ────────────────────────────────────────────────────────────────────────────
async def run(conn, pending: list, args):
    limiter = RateLimiter(RATE_PER_SEC)
    ban = BanDetector(ABORT_AFTER_CONSECUTIVE_429, limiter)
    queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": len(pending),
        "done": 0,
        "priorities": {},
        "secured_party_types": {},
        "errors": 0,
        "requests": 0,
    }
    buffer = []
    lock = asyncio.Lock()
    t0 = time.time()
    aborted = asyncio.Event()

    async def flush(force=False):
        nonlocal buffer
        if not buffer or (len(buffer) < BATCH_SIZE and not force):
            return
        batch, buffer = buffer, []
        save_batch(conn, batch)
        stats["elapsed_s"] = round(time.time() - t0, 1)
        stats["requests"] = limiter.requests
        stats["rate_req_s"] = round(limiter.requests / max(time.time() - t0, 0.001), 2)
        stats["rate_carriers_s"] = round(stats["done"] / max(time.time() - t0, 0.001), 2)
        save_checkpoint(stats)

    async def worker(client, wid: int):
        while not aborted.is_set():
            try:
                dot, name = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                res = await scrape_carrier(client, dot, name, limiter, ban)
            except RateLimited:
                log("[ABORT] 429/403 sostenido: paramos para no quemar la IP")
                aborted.set()
                queue.task_done()
                return
            except Exception as e:  # noqa: BLE001 - un bug no debe matar la corrida
                res = ScrapeResult(dot_number=dot, legal_name=name,
                                   error=f"{type(e).__name__}: {e}")

            async with lock:
                stats["done"] += 1
                p = res.alert_priority
                stats["priorities"][p] = stats["priorities"].get(p, 0) + 1
                if res.error:
                    stats["errors"] += 1
                for f in res.filings:
                    t = f.secured_party_type
                    stats["secured_party_types"][t] = \
                        stats["secured_party_types"].get(t, 0) + 1
                buffer.append(res)

                n = stats["done"]
                if n <= 20 or n % 25 == 0:
                    rate = limiter.requests / max(time.time() - t0, 0.001)
                    flag = f"ERROR({res.error})" if res.error else \
                           f"{p} {len(res.filings)} filing(s)"
                    log(f"[{n}/{len(pending)}] {dot} {name[:38]:<38} {flag}"
                        f"   {rate:.2f} req/s")
                await flush()
            queue.task_done()

    # Un solo AsyncClient compartido: httpx es seguro para uso concurrente y asi
    # cebamos las cookies de Incapsula UNA vez (la pagina pesa ~1.3 MB; con un
    # cliente por worker eran 20 descargas antes de la primera busqueda) y todos
    # los workers reusan el mismo pool de conexiones.
    limits = httpx.Limits(max_connections=CONCURRENT,
                          max_keepalive_connections=CONCURRENT)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True,
                                 timeout=TIMEOUT, limits=limits) as client:
        try:
            await client.get(CA_PAGE_URL, timeout=TIMEOUT)
        except httpx.HTTPError:
            pass

        n_workers = min(CONCURRENT, max(len(pending), 1))
        workers = [asyncio.create_task(worker(client, i)) for i in range(n_workers)]
        await asyncio.gather(*workers)
        async with lock:
            await flush(force=True)

    stats["elapsed_s"] = round(time.time() - t0, 1)
    stats["rate_req_s"] = round(stats["requests"] / max(time.time() - t0, 0.001), 2)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    stats["aborted"] = aborted.is_set()
    stats["waf_first_block_at_request"] = ban.first_block_at_request
    stats["waf_total_blocks"] = ban.total_blocks
    stats["rate_configured"] = RATE_PER_SEC
    stats["workers_configured"] = CONCURRENT
    save_checkpoint(stats)
    return stats


def report(conn, stats: dict):
    log("\n" + "=" * 64)
    log("RESUMEN CA")
    log("=" * 64)
    log(f"carriers procesados : {stats['done']:,}")
    log(f"tiempo              : {stats['elapsed_s']}s")
    log(f"requests HTTP       : {stats['requests']:,}")
    log(f"rate real           : {stats['rate_req_s']} req/s  (techo {RATE_PER_SEC})")
    log(f"rate carriers       : {stats['rate_carriers_s']} carriers/s")
    log(f"errores             : {stats['errors']:,}")

    # Medicion del umbral del WAF — es lo que decide si bajar el rate sirve de
    # algo o si hace falta rotar IP.
    fb = stats.get("waf_first_block_at_request")
    if fb:
        log(f"\n[WAF] primer bloqueo en request #{fb:,} | "
            f"bloqueos totales: {stats.get('waf_total_blocks', 0):,} | "
            f"config: {stats.get('rate_configured')} req/s x "
            f"{stats.get('workers_configured')} workers")
        log("      Compara este numero con el de la corrida a 2.0 req/s (~2,600):")
        log("      similar -> Incapsula cuenta VOLUMEN, bajar el rate no salva -> proxies")
        log("      mucho mayor -> es sensible al rate, seguir a ritmo lento")
    elif not stats.get("aborted"):
        log("\n[WAF] sin bloqueos en esta corrida")

    log("\nDistribucion de prioridades:")
    order = ["P1", "P2a", "P2b", "P2c", "P3", "P4", "ERROR"]
    for p in order:
        n = stats["priorities"].get(p, 0)
        if n:
            pct = 100 * n / max(stats["done"], 1)
            log(f"  {p:<6} {n:>7,}  ({pct:5.1f}%)")

    log("\nsecured_party_type (filings):")
    for t, n in sorted(stats["secured_party_types"].items(), key=lambda x: -x[1]):
        log(f"  {t:<8} {n:>7,}")

    # Verificacion dura: ningun error puede haber acabado en ucc_filings.
    leaked = conn.execute("""
        SELECT COUNT(*) FROM ucc_filings f
        JOIN scrape_log l ON l.dot_number = f.dot_number
        WHERE f.state_registry = ? AND l.status = 'error'
    """, (STATE,)).fetchone()[0]
    log(f"\nErrores filtrados como P1 en ucc_filings: {leaked}"
        f"  {'OK' if leaked == 0 else '<-- REVISAR'}")


def main():
    # run() y report() leen RATE_PER_SEC/CONCURRENT como globals; declararlos
    # aqui arriba permite que --rate/--workers los sobreescriban.
    global RATE_PER_SEC, CONCURRENT

    ap = argparse.ArgumentParser(description="Scraper UCC de California (bizfileonline)")
    ap.add_argument("--test", type=int, metavar="N", help="corre solo N carriers")
    ap.add_argument("--retry-errors", action="store_true",
                    help="reintenta los carriers CA con status='error'")
    ap.add_argument("--alerts-only", action="store_true",
                    help="solo P2b, P2c y carriers nuevos")
    ap.add_argument("--reset", action="store_true", help="borra el checkpoint")
    ap.add_argument("--rate", type=float, metavar="R",
                    help=f"techo de requests/segundo (default {RATE_PER_SEC})")
    ap.add_argument("--workers", type=int, metavar="N",
                    help=f"workers simultaneos (default {CONCURRENT})")
    args = ap.parse_args()

    if args.rate:
        RATE_PER_SEC = args.rate
    if args.workers:
        CONCURRENT = args.workers

    if args.reset and CHECKPOINT.exists():
        CHECKPOINT.unlink()
        log("[reset] checkpoint borrado")

    conn = init_db(DB_FILE)
    carriers = load_carriers()
    pending = select_pending(conn, carriers, args)

    if not pending:
        log("[run] nada pendiente")
        return

    log(f"[run] {len(pending):,} carriers | {CONCURRENT} workers | "
        f"techo {RATE_PER_SEC} req/s\n")
    stats = asyncio.run(run(conn, pending, args))
    report(conn, stats)
    conn.close()


if __name__ == "__main__":
    main()
