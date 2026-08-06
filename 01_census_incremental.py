"""
movik/01_census_incremental.py
==============================
Descarga incremental del FMCSA Company Census File.

- Primera vez: descarga TODO (igual que census_pull.py pero con estado guardado)
- Siguientes veces: solo descarga registros con mcs150_date >= última corrida

Uso:
  python 01_census_incremental.py          # pull incremental
  python 01_census_incremental.py --full   # forzar pull completo
  python 01_census_incremental.py --check  # solo mostrar cuántos registros nuevos hay

Genera/actualiza: census_file_full.csv
"""

import requests
import csv
import sys
import time
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

# La consola de Windows usa cp1252 y revienta con los emojis de los logs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATASET_ID   = "az4n-8mr2"
BASE_URL     = f"https://data.transportation.gov/resource/{DATASET_ID}.json"
METADATA_URL = f"https://data.transportation.gov/api/views/{DATASET_ID}.json"
OUTPUT_FILE  = "census_file_full.csv"
STATE_FILE   = "census_state.json"   # guarda timestamp del último pull
PAGE_SIZE    = 50_000
ORDER_FIELD  = "dot_number"
MAX_RETRIES  = 5

# Columna que usamos como proxy de "actualización"
# mcs150_date = fecha del último formulario MCS-150 del carrier (formato YYYYMMDD)
DATE_FIELD   = "mcs150_date"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_schema(session) -> list[str]:
    """Trae columnas desde la metadata del dataset."""
    r = session.get(METADATA_URL, timeout=30)
    r.raise_for_status()
    meta = r.json()
    fields = [c["fieldName"] for c in meta["columns"]
              if not c["fieldName"].startswith(":")]
    log(f"Schema: {len(fields)} columnas detectadas")
    return fields


def get_total_count(session, where_clause: str = "") -> int:
    params = {"$select": "count(*)"}
    if where_clause:
        params["$where"] = where_clause
    r = session.get(BASE_URL, params=params, timeout=30)
    r.raise_for_status()
    return int(r.json()[0]["count"])


def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def fetch_page(session, offset: int, limit: int, where_clause: str = "") -> list[dict]:
    params = {
        "$limit":  limit,
        "$offset": offset,
        "$order":  ORDER_FIELD,
    }
    if where_clause:
        params["$where"] = where_clause

    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(BASE_URL, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            log(f"  HTTP {r.status_code} en offset {offset}, intento {attempt+1}")
        except requests.exceptions.RequestException as e:
            log(f"  Error de red en offset {offset}, intento {attempt+1}: {e}")
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Fallo definitivo en offset {offset}")


def pull(session, fieldnames: list[str], where_clause: str = "",
         append: bool = False, out_file: str | None = None) -> int:
    """
    Descarga páginas y escribe al CSV.
    Retorna número total de filas escritas.

    out_file permite escribir a otro destino que no sea el censo completo: lo
    usa --delta-only para dejar solo lo nuevo en un archivo aparte.
    """
    out_file = out_file or OUTPUT_FILE
    total_api = get_total_count(session, where_clause)
    log(f"Registros a descargar: {total_api}")

    if total_api == 0:
        log("Sin registros nuevos.")
        # Aun sin filas se escribe la cabecera: quien lo consuma despues
        # (05_load_carriers_db.py) necesita un CSV valido, no un archivo ausente.
        if not append:
            with open(out_file, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fieldnames,
                               extrasaction="ignore").writeheader()
        return 0

    mode = "a" if append else "w"
    write_header = (not append) or (not Path(out_file).exists())

    written = 0
    offset  = 0

    with open(out_file, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        while True:
            rows = fetch_page(session, offset, PAGE_SIZE, where_clause)
            if not rows:
                break

            for row in rows:
                writer.writerow(row)

            f.flush()
            written  += len(rows)
            offset   += PAGE_SIZE

            pct = round(written / total_api * 100, 1)
            log(f"  {written:,} / {total_api:,} ({pct}%)")

            if len(rows) < PAGE_SIZE:
                break

            time.sleep(0.3)

    return written


def get_max_date_in_csv(path: str | None = None) -> str | None:
    """Lee un CSV del censo y encuentra el mcs150_date máximo."""
    csv_path = Path(path or OUTPUT_FILE)
    if not csv_path.exists():
        return None

    log(f"Escaneando {csv_path.name} para encontrar max({DATE_FIELD})...")
    max_date = None
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if DATE_FIELD not in (reader.fieldnames or []):
            return None
        for row in reader:
            d = row.get(DATE_FIELD, "").strip()
            if d and (max_date is None or d > max_date):
                max_date = d
    log(f"max({DATE_FIELD}) en CSV existente: {max_date}")
    return max_date


def main():
    parser = argparse.ArgumentParser(description="FMCSA Census Pull Incremental")
    parser.add_argument("--full",  action="store_true", help="Forzar pull completo")
    parser.add_argument("--check", action="store_true", help="Solo contar registros nuevos")
    parser.add_argument("--delta-only", nargs="?", const="census_delta.csv",
                        metavar="FILE",
                        help="Descargar SOLO los registros nuevos a FILE "
                             "(default census_delta.csv) sin tocar el censo "
                             "completo. Para CI, donde el CSV de 1,7 GB no está.")
    args = parser.parse_args()

    session = requests.Session()
    state   = load_state()

    # ── Modo delta: para GitHub Actions ────────────────────────────────────
    # El workspace de un runner nace vacío, así que census_file_full.csv nunca
    # está y la heurística de abajo (`not OUTPUT_FILE.exists()` → pull completo)
    # bajaría las 4,4M filas cada día. Meter el CSV en el artifact tampoco es
    # opción: son 1,7 GB contra los 500 MB que da el plan free.
    #
    # Aquí solo se descarga lo nuevo, se deja en un CSV chico que
    # 05_load_carriers_db.py sabe leer con --csv, y el censo completo ni se
    # menciona. El watermark sale del propio delta, no de escanear 1,7 GB.
    if args.delta_only:
        last_date = state.get("last_max_date")
        if not last_date:
            log(f"ERROR: --delta-only necesita last_max_date en {STATE_FILE}. "
                f"Corre un pull completo una vez y conserva ese archivo.")
            sys.exit(1)

        where = f"{DATE_FIELD} >= '{last_date}'"
        log(f"PULL DELTA — where: {where} → {args.delta_only}")

        fieldnames = get_schema(session)
        t0 = time.time()
        written = pull(session, fieldnames, where_clause=where,
                       append=False, out_file=args.delta_only)

        # Sin filas nuevas el watermark no se mueve: reescribirlo con None
        # dejaría el estado inservible para la corrida siguiente.
        if written:
            nuevo = get_max_date_in_csv(args.delta_only)
            if nuevo:
                state["last_max_date"] = nuevo
            state["last_pull_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)

        log(f"✅ Delta listo en {(time.time()-t0)/60:.1f} min — "
            f"{written:,} filas en {args.delta_only}")
        log(f"   Watermark: {state['last_max_date']}")
        return

    # ── Decidir si es pull completo o incremental ──────────────────────────
    is_full = args.full or not Path(OUTPUT_FILE).exists()
    where   = ""

    if not is_full:
        # Buscar la fecha más alta que ya tenemos
        last_date = state.get("last_max_date") or get_max_date_in_csv()
        if last_date:
            # Traer todo lo que tenga mcs150_date >= last_date
            # (el >= incluye el día del último pull por seguridad)
            where = f"{DATE_FIELD} >= '{last_date}'"
            log(f"PULL INCREMENTAL — where: {where}")
        else:
            is_full = True

    if is_full:
        log("PULL COMPLETO — descargando las 4.4M filas")

    # ── Solo contar ────────────────────────────────────────────────────────
    if args.check:
        fieldnames = get_schema(session)
        count = get_total_count(session, where)
        if is_full:
            log(f"Registros totales en el dataset: {count:,}")
        else:
            log(f"Registros nuevos/actualizados desde {state.get('last_max_date')}: {count:,}")
        return

    # ── Descargar ──────────────────────────────────────────────────────────
    fieldnames = get_schema(session)
    t0         = time.time()

    if is_full:
        written = pull(session, fieldnames, where_clause="", append=False)
    else:
        # Modo incremental: upsert simplificado
        # 1. Descarga los nuevos a un archivo temporal
        # 2. Lee los dot_numbers nuevos
        # 3. Reescribe el CSV principal ignorando los dot_numbers repetidos
        #    y appending los nuevos al final
        log("Descargando incrementales a census_incremental_tmp.csv...")
        tmp_file = "census_incremental_tmp.csv"

        with open(tmp_file, "w", newline="", encoding="utf-8") as tf:
            writer = csv.DictWriter(tf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            offset = 0
            while True:
                rows = fetch_page(session, offset, PAGE_SIZE, where)
                if not rows:
                    break
                for row in rows:
                    writer.writerow(row)
                offset += PAGE_SIZE
                log(f"  tmp: {offset:,} filas")
                if len(rows) < PAGE_SIZE:
                    break
                time.sleep(0.3)

        # Leer los dot_numbers del incremental
        new_dots = set()
        new_rows = []
        with open(tmp_file, encoding="utf-8") as tf:
            for row in csv.DictReader(tf):
                new_dots.add(row["dot_number"])
                new_rows.append(row)
        log(f"  {len(new_dots)} carriers en el incremental")

        # Reescribir CSV principal: primero los que NO están en new_dots,
        # luego los nuevos/actualizados
        log(f"Actualizando {OUTPUT_FILE}...")
        existing_path = Path(OUTPUT_FILE)
        tmp_main = OUTPUT_FILE + ".tmp"

        with open(tmp_main, "w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            kept = 0
            with open(existing_path, encoding="utf-8") as inp:
                for row in csv.DictReader(inp):
                    if row.get("dot_number") not in new_dots:
                        writer.writerow(row)
                        kept += 1
            # Agregar los nuevos
            for row in new_rows:
                writer.writerow(row)
            written = kept + len(new_rows)
            log(f"  CSV actualizado: {kept:,} existentes + {len(new_rows):,} nuevos = {written:,} total")

        os.replace(tmp_main, OUTPUT_FILE)
        os.remove(tmp_file)

    # ── Encontrar nuevo max_date y guardar estado ──────────────────────────
    new_max_date = get_max_date_in_csv()
    state["last_max_date"]  = new_max_date
    state["last_pull_at"]   = datetime.now(timezone.utc).isoformat()
    state["total_rows"]     = written
    save_state(state)

    elapsed = time.time() - t0
    log(f"✅ Listo en {elapsed/60:.1f} min — {written:,} filas — estado guardado en {STATE_FILE}")
    log(f"   Próxima corrida será incremental desde: {new_max_date}")


if __name__ == "__main__":
    main()