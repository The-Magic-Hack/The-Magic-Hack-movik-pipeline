"""
movik/generate_seed.py
======================
Genera Movikapp/src/data/seed.json: una muestra de movik.db lo bastante rica
para que la demo desplegada se vea y se comporte como la app real.

Por qué existe
--------------
Vercel es serverless: no hay disco donde poner movik.db (91 MB), y aunque lo
hubiera, un bundle con la DB entera es inviable. La app corre en modo dual —
si encuentra ../movik.db lee SQLite; si no, cae a este seed.

Replica la misma lógica de prioridad que src/lib/db.ts:
  - scrape_log es la fuente de verdad: sin fila ahí el carrier es "Sin datos",
    nunca P1 (marcarlo P1 lo volvería un lead falso).
  - El filing representativo es el que vence antes entre los activos, que es
    el mismo que determinó alert_priority en ucc_scraper.py.
  - days se recalcula contra HOY, no contra el día del scrape.

Uso:
  python generate_seed.py
  python generate_seed.py --carriers 1000 --alerts 500
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_FILE = "movik.db"
OUT_FILE = "Movikapp/src/data/seed.json"
STATES = ["FL", "CA", "NC"]

# Orden de urgencia con el que el proyecto presenta las prioridades.
PRIORITY_RANK = {"P1": 0, "P2b": 1, "P2c": 2, "P2a": 3, "P3": 4, "P4": 5}

# Cuota por prioridad en la muestra de alertas. No es proporcional a propósito:
# tomar las primeras 500 por orden de urgencia daría 500 P1 con días=NULL y la
# demo no mostraría ni un P2b. Se sobre-representan las prioridades escasas
# para que todos los filtros devuelvan filas.
ALERT_QUOTA = {"P1": 150, "P2b": 60, "P2c": 60, "P2a": 80, "P3": 60, "P4": 90}

# ── Clasificación del secured party ─────────────────────────────────────────
# Puerto de ca_ucc_scraper.classify_secured_party. El orden importa: las listas
# se solapan. 'AMERICA' es el keyword más laxo (matchearía "AMERICAN TRUST
# FUND"), así que bank va después de trust; y factor va primero porque
# "TRIUMPH BUSINESS CAPITAL" es un factor, no un banco.
_FACTOR_KW = [
    "RTS", "OTR", "RIVIERA", "TRIUMPH", "APEX", "ECAPITAL", "TAFS",
    "PORTER FREIGHT", "AXLE", "ALTLINE", "PARAGON",
    "FACTORING", "FACTOR", "RECEIVABLE", "FUNDING",
]
_TRUST_KW = ["TRUST", "FUND", "HOLDINGS", "INVESTMENT", "CAPITAL TRUST"]
_BANK_KW = [
    "BANK", "SBA", "WELLS", "CHASE", "AMERICA", "CAPITAL ONE",
    "CITIBANK", "TD BANK", "BANCORP", "CREDIT UNION",
    "SMALL BUSINESS ADMINISTRATION",
]


def _compile(words):
    return [re.compile(r"\b" + re.escape(w) + r"\b") for w in words]


_FACTOR_RE, _TRUST_RE, _BANK_RE = _compile(_FACTOR_KW), _compile(_TRUST_KW), _compile(_BANK_KW)


def classify_secured_party(name):
    if not name:
        return "other"
    n = name.upper()
    for group, label in ((_FACTOR_RE, "factor"), (_TRUST_RE, "trust"), (_BANK_RE, "bank")):
        if any(rx.search(n) for rx in group):
            return label
    return "other"


# ── Caché de alertas: mismo SQL que src/lib/db.ts ───────────────────────────
DAYS_LIVE = """
  COALESCE(
    CAST(
      julianday(
        substr(f.expires_date, 7, 4) || '-' ||
        substr(f.expires_date, 1, 2) || '-' ||
        substr(f.expires_date, 4, 2)
      ) - julianday('now', 'start of day')
    AS INTEGER),
    f.days_to_expiry
  )
"""

BUILD_CACHE = f"""
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
  COUNT(*) AS n_filings
FROM ucc_filings u
WHERE u.match_found = 1
GROUP BY u.dot_number;

CREATE TEMP TABLE mv_alerts AS
SELECT
  c.dot_number, c.legal_name, c.phy_state, c.phy_city, c.phy_zip, c.phone,
  c.mcs150_date,
  COALESCE(c.power_units, c.truck_units, 0) AS units,
  CASE
    WHEN s.status = 'error' THEN 'Error'
    WHEN s.status = 'not_found' THEN 'P1'
    WHEN s.status = 'found' AND r.rep_id IS NULL THEN 'Error'
    WHEN s.status = 'found' THEN COALESCE(NULLIF(f.alert_priority, ''), 'P4')
    ELSE 'Sin datos'
  END AS priority,
  {DAYS_LIVE} AS days,
  NULLIF(f.expires_date, '') AS expires_date,
  NULLIF(f.date_filed, '') AS date_filed,
  NULLIF(f.ucc_number, '') AS ucc_number,
  NULLIF(f.secured_party, '') AS secured_party,
  f.secured_party_type,
  COALESCE(r.n_filings, 0) AS n_filings
FROM carriers c
LEFT JOIN scrape_log s ON s.dot_number = c.dot_number
LEFT JOIN mv_rep r ON r.dot_number = c.dot_number
LEFT JOIN ucc_filings f ON f.id = r.rep_id;

CREATE INDEX temp.i_prio ON mv_alerts(priority);
CREATE INDEX temp.i_state ON mv_alerts(phy_state);
"""


def build_city(row):
    bits = [f"{row['phy_city']}," if row["phy_city"] else "", row["phy_state"], row["phy_zip"]]
    return " ".join(b for b in bits if b).strip()


def to_alert(row):
    return {
        "dot": str(row["dot_number"]),
        "name": row["legal_name"] or "(Sin nombre)",
        "city": build_city(row),
        "state": row["phy_state"] or "—",
        "units": row["units"] or 0,
        "priority": row["priority"],
        "days": row["days"],
        "secured": row["secured_party"],
        "ucc": row["ucc_number"],
        "expires": row["expires_date"],
        "filed": row["date_filed"],
        "phone": row["phone"] or None,
        "spType": row["secured_party_type"],
        "nFilings": row["n_filings"] or 0,
    }


def to_carrier(row):
    return {
        "dot": str(row["dot_number"]),
        "name": row["legal_name"] or "(Sin nombre)",
        "state": row["phy_state"] or "—",
        "units": row["units"] or 0,
        "phone": row["phone"] or "—",
        "mcs150": row["mcs150_date"] or "—",
        "potential": row["priority"],
    }


def main():
    p = argparse.ArgumentParser(description="Generar seed.json para la demo")
    p.add_argument("--db", default=DB_FILE)
    p.add_argument("--out", default=OUT_FILE)
    p.add_argument("--alerts", type=int, default=500)
    p.add_argument("--carriers", type=int, default=2000)
    p.add_argument("--ucc", type=int, default=300,
                   help="Carriers buscables en /busqueda")
    args = p.parse_args()

    if not Path(args.db).exists():
        print(f"No existe {args.db}.")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(f"Leyendo {args.db} y construyendo la caché de alertas...")
    conn.executescript(BUILD_CACHE)

    # ── metrics ─────────────────────────────────────────────────────────────
    counts = {r["priority"]: r["n"] for r in
              conn.execute("SELECT priority, COUNT(*) AS n FROM mv_alerts GROUP BY priority")}
    total = sum(counts.values())
    processed = sum(counts.get(k, 0) for k in PRIORITY_RANK)
    metrics = {k: counts.get(k, 0) for k in ["P1", "P2a", "P2b", "P2c", "P3", "P4"]}
    metrics.update({
        "total": total,
        "processed": processed,
        "sinDatos": counts.get("Sin datos", 0),
        "errors": counts.get("Error", 0),
        "p1Rate": round(metrics["P1"] / processed * 100) if processed else 0,
    })

    # ── alerts: cuota por prioridad, después ordenado por urgencia ──────────
    alerts = []
    scale = args.alerts / sum(ALERT_QUOTA.values())
    for prio, quota in ALERT_QUOTA.items():
        n = max(1, round(quota * scale))
        rows = conn.execute(
            """SELECT * FROM mv_alerts WHERE priority = ?
               ORDER BY days IS NULL, days ASC, legal_name ASC LIMIT ?""",
            (prio, n),
        ).fetchall()
        alerts.extend(to_alert(r) for r in rows)
    alerts.sort(key=lambda a: (PRIORITY_RANK.get(a["priority"], 9),
                               a["days"] is None, a["days"] or 0, a["name"]))

    # ── carriers: muestra balanceada entre estados Y entre prioridades ──────
    # Balancear sólo por estado no basta: tomar los primeros N por orden
    # alfabético trae casi puros "Sin datos" (el 80% de la DB), y el filtro
    # "Potencial" de /carriers quedaría sin filas que devolver en la demo.
    carriers = []
    per_state = args.carriers // len(STATES)
    for st in STATES:
        prios = [r["priority"] for r in conn.execute(
            "SELECT priority FROM mv_alerts WHERE phy_state = ? GROUP BY priority",
            (st,),
        )]
        quota = max(1, per_state // max(1, len(prios)))
        picked, seen = [], set()
        for prio in prios:
            for r in conn.execute(
                """SELECT * FROM mv_alerts WHERE phy_state = ? AND priority = ?
                   ORDER BY legal_name ASC LIMIT ?""",
                (st, prio, quota),
            ):
                picked.append(r)
                seen.add(r["dot_number"])
        # Si alguna prioridad tenía menos filas que su cuota, rellenar.
        if len(picked) < per_state:
            for r in conn.execute(
                "SELECT * FROM mv_alerts WHERE phy_state = ? ORDER BY legal_name ASC LIMIT ?",
                (st, per_state * 3),
            ):
                if len(picked) >= per_state:
                    break
                if r["dot_number"] not in seen:
                    picked.append(r)
                    seen.add(r["dot_number"])
        carriers.extend(to_carrier(r) for r in picked[:per_state])
        mix = {}
        for r in picked[:per_state]:
            mix[r["priority"]] = mix.get(r["priority"], 0) + 1
        print(f"   carriers {st}: {min(len(picked), per_state):,}  {mix}")
    # Mismo orden que SQLite (BINARY, por code unit), no localeCompare.
    carriers.sort(key=lambda c: c["name"])

    # ── topParties ──────────────────────────────────────────────────────────
    top_parties = [
        {"label": r["label"], "value": r["value"]}
        for r in conn.execute(
            """SELECT secured_party AS label, COUNT(DISTINCT dot_number) AS value
                 FROM ucc_filings
                WHERE match_found = 1 AND secured_party IS NOT NULL AND secured_party <> ''
                GROUP BY secured_party ORDER BY value DESC LIMIT 10"""
        )
    ]

    # ── securedTypes ────────────────────────────────────────────────────────
    secured_types = {"factor": 0, "trust": 0, "bank": 0, "other": 0}
    for r in conn.execute(
        """SELECT secured_party, secured_party_type, COUNT(DISTINCT dot_number) AS n
             FROM ucc_filings
            WHERE match_found = 1 AND secured_party IS NOT NULL AND secured_party <> ''
            GROUP BY secured_party, secured_party_type"""
    ):
        key = r["secured_party_type"] or classify_secured_party(r["secured_party"])
        secured_types[key] = secured_types.get(key, 0) + r["n"]

    # ── stateStats ──────────────────────────────────────────────────────────
    totals = {r["phy_state"]: r["n"] for r in
              conn.execute("SELECT phy_state, COUNT(*) AS n FROM mv_alerts GROUP BY phy_state")}
    # Desglose por estado y status. Sólo cuenta lo que está en el censo, igual
    # que getScraperStatus en la app, para que ambos reporten el mismo %.
    by_status = {}
    for r in conn.execute(
        """SELECT COALESCE(f.state_registry, c.phy_state, 'FL') AS code,
                  s.status AS status, COUNT(*) AS n, MAX(s.scraped_at) AS last_at
             FROM scrape_log s
             LEFT JOIN carriers c ON c.dot_number = s.dot_number
             LEFT JOIN (SELECT DISTINCT dot_number, state_registry FROM ucc_filings
                         WHERE match_found = 1) f ON f.dot_number = s.dot_number
            WHERE c.dot_number IS NOT NULL
            GROUP BY code, s.status"""
    ):
        by_status.setdefault(r["code"], []).append(dict(r))

    state_stats = {}
    for st in STATES:
        rows = by_status.get(st, [])
        pick = lambda k: next((r["n"] for r in rows if r["status"] == k), 0)  # noqa: E731
        t = totals.get(st, 0)
        sc = sum(r["n"] for r in rows)
        state_stats[st] = {
            "total": t,
            "scraped": sc,
            "pct": round(sc / t * 1000) / 10 if t else None,
            "found": pick("found"),
            "notFound": pick("not_found"),
            "errors": pick("error"),
            "lastScrapedAt": max((r["last_at"] for r in rows if r["last_at"]), default=None),
        }

    # ── uccSearch: lo que consulta /busqueda ────────────────────────────────
    # Sin tabla ucc_filings en la demo, la búsqueda necesita su propio índice.
    # Los 4 nombres que la página ofrece como ejemplos van forzados; el resto
    # se rellena con carriers que tengan filings con detalle (fecha y secured
    # party), que son los que se ven bien en pantalla.
    examples = ["PFKR CARRIER LLC", "TONYS TRANSPORT SOLUTIONS LLC",
                "ELK MOTOR CORP", "SIERRA PREMIER TRUCKING LLC"]
    dots = []
    for name in examples:
        row = conn.execute(
            "SELECT dot_number FROM ucc_filings WHERE UPPER(legal_name_searched) = ? LIMIT 1",
            (name,),
        ).fetchone()
        if row:
            dots.append(row["dot_number"])
        else:
            print(f"   aviso: el ejemplo '{name}' no está en ucc_filings")
    for r in conn.execute(
        f"""SELECT DISTINCT dot_number FROM ucc_filings
             WHERE match_found = 1 AND secured_party <> '' AND expires_date <> ''
             LIMIT {max(0, args.ucc - len(dots))}"""
    ):
        if r["dot_number"] not in dots:
            dots.append(r["dot_number"])

    ucc_search = []
    for dot in dots:
        rows = conn.execute(
            f"""SELECT ucc_number, filing_status, date_filed, expires_date, days_to_expiry,
                       secured_party, secured_party_addr, secured_party_type, state_registry,
                       legal_name_searched, match_found,
                       CAST(julianday(substr(expires_date,7,4)||'-'||substr(expires_date,1,2)
                            ||'-'||substr(expires_date,4,2))
                            - julianday('now','start of day') AS INTEGER) AS days_live
                  FROM ucc_filings WHERE dot_number = ? ORDER BY id""",
            (dot,),
        ).fetchall()
        if not rows:
            continue
        ucc_search.append({
            "name": rows[0]["legal_name_searched"],
            "dot": str(dot),
            "filings": [
                {
                    "num": r["ucc_number"],
                    "status": r["filing_status"] or "—",
                    "filed": r["date_filed"] or None,
                    "lapse": r["expires_date"] or None,
                    "days": r["days_live"] if r["days_live"] is not None else r["days_to_expiry"],
                    "party": r["secured_party"] or None,
                    "addr": r["secured_party_addr"] or None,
                    "type": r["secured_party_type"] or classify_secured_party(r["secured_party"]),
                    "registry": r["state_registry"] or "—",
                }
                for r in rows if r["match_found"] == 1 and r["ucc_number"]
            ],
        })

    conn.close()

    payload = {
        "metrics": metrics,
        "alerts": alerts,
        "carriers": carriers,
        "topParties": top_parties,
        "securedTypes": secured_types,
        "stateStats": state_stats,
        "uccSearch": ucc_search,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size = out.stat().st_size
    print(f"\n{out}")
    print(f"   {size:,} bytes ({size / 1024 / 1024:.2f} MB)")
    print(f"   alerts   : {len(alerts):,}")
    print(f"   carriers : {len(carriers):,}")
    print(f"   uccSearch: {len(ucc_search):,}")
    print(f"   metrics  : {metrics}")
    print(f"   stateStats: {state_stats}")
    if size > 5 * 1024 * 1024:
        print("\n   Pasa de 5 MB: vuelve a correr con --carriers 1000")


if __name__ == "__main__":
    main()
