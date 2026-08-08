"""
movik/ca_resume.py
==================
Espera a que Incapsula suelte nuestra IP y entonces reanuda ca_ucc_scraper.py
a ritmo lento.

Por que existe: tras el bloqueo del 24/07/2026 la IP queda marcada y cualquier
POST al API devuelve 403 con challenge JS. Relanzar el scraper de inmediato
solo refresca la marca. Este script:

  1. Espera un enfriamiento inicial (COOLDOWN_MIN).
  2. Sondea con UNA sola request cada PROBE_EVERY_MIN hasta recibir 200.
     Una request aislada cada 10 min no reconstruye reputacion negativa.
  3. Cuando el portal responde, lanza el scraper con el rate/workers indicados
     y termina. El scraper reanuda solo desde scrape_log (los ~2,300 ya hechos
     no se repiten).

Uso:
  python -u ca_resume.py [--cooldown 60] [--rate 0.4] [--workers 5]
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import ca_ucc_scraper as S  # noqa: E402

PROBE_EVERY_MIN = 10
MAX_WAIT_MIN = 360          # 6 h; si en ese tiempo no suelta, no es transitorio


def emit(msg: str):
    print(f"{datetime.now().strftime('%H:%M')} {msg}", flush=True)


def probe() -> int:
    """Una sola busqueda real. Devuelve el status HTTP (0 = fallo de red)."""
    try:
        with httpx.Client(headers=S.HEADERS, follow_redirects=True, timeout=30) as c:
            c.get(S.CA_PAGE_URL)
            r = c.post(S.CA_SEARCH_URL, json=S.build_body("DILPREET TRANSPORT INC"))
            return r.status_code
    except httpx.HTTPError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cooldown", type=int, default=60, help="minutos de espera inicial")
    ap.add_argument("--rate", type=float, default=0.4)
    ap.add_argument("--workers", type=int, default=5)
    # Para usarlo como paso de un workflow: espera a que el portal responda y
    # sale (0 = via libre, 1 = sigue bloqueado), sin lanzar el scraper. Ahi el
    # scraper es el paso siguiente, asi GitHub registra su salida y su duracion
    # en vez de perderlos en un Popen que nadie espera.
    ap.add_argument("--probe-only", action="store_true",
                    help="Solo esperar via libre y salir; no lanza el scraper")
    # En GitHub Actions esperar sale carisimo: el sondeo de la primera corrida
    # se comio 160 de los ~350 min del run. Y como cada run trae runner nuevo,
    # rendirse pronto y dejar que lo intente el siguiente es mas barato que
    # aguantar horas contra una IP que quiza no ceda.
    ap.add_argument("--max-wait", type=int, default=MAX_WAIT_MIN, metavar="M",
                    help=f"Minutos maximos de sondeo antes de rendirse "
                         f"(default {MAX_WAIT_MIN})")
    a = ap.parse_args()

    emit(f"[RESUME] enfriamiento de {a.cooldown} min antes del primer sondeo")
    time.sleep(a.cooldown * 60)

    waited = a.cooldown
    while True:
        st = probe()
        if st == 200:
            emit(f"[RESUME] portal responde 200 tras {waited} min - reanudando")
            break
        emit(f"[RESUME] sondeo: HTTP {st} - sigue bloqueado ({waited} min esperados)")
        if waited >= a.max_wait:
            emit(f"[BLOQUEADO] {a.max_wait} min sin via libre. Esta IP no sirve.")
            # 2, no 1: "esta IP esta bloqueada" no es un error del programa. El
            # workflow lo distingue para terminar el run en limpio y dejar que
            # el siguiente lo intente con otro runner, en vez de pintarse de
            # rojo o de gastar el presupuesto entero esperando.
            sys.exit(2 if a.probe_only else 0)
        time.sleep(PROBE_EVERY_MIN * 60)
        waited += PROBE_EVERY_MIN

    if a.probe_only:
        return

    log_path = BASE / "ca_scrape_run.log"
    err_path = BASE / "ca_scrape_err.log"
    emit(f"[RESUME] lanzando scraper a {a.rate} req/s x {a.workers} workers")
    with open(log_path, "a", encoding="utf-8") as out, \
         open(err_path, "a", encoding="utf-8") as err:
        out.write(f"\n===== reanudado {datetime.now().isoformat()} "
                  f"@ {a.rate} req/s x {a.workers} workers =====\n")
        out.flush()
        p = subprocess.Popen(
            [sys.executable, "-u", "ca_ucc_scraper.py",
             "--rate", str(a.rate), "--workers", str(a.workers)],
            cwd=str(BASE), stdout=out, stderr=err,
        )
    emit(f"[RESUME] scraper lanzado con pid={p.pid}")


if __name__ == "__main__":
    main()
