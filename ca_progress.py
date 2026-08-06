"""
movik/ca_progress.py
====================
Imprime el avance de CA en markdown, para el resumen del workflow.

Vive en un archivo y no incrustado en el YAML porque el runner de CA es
Windows: alli los `run:` se interpretan con PowerShell, y el heredoc
`python - <<'PY'` que servia en los runners Linux no existe.

Uso:
  python ca_progress.py >> $env:GITHUB_STEP_SUMMARY
"""

import json
import pathlib
import sqlite3
import sys

# La consola de Windows usa cp1252 y revienta con los guiones largos y los
# emojis. Mismo guard que 01_census_incremental.py y ca_ucc_scraper.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = pathlib.Path(__file__).resolve().parent


def main():
    db = BASE / "movik.db"
    if not db.exists():
        print("- Sin movik.db: no hay avance que reportar")
        return

    conn = sqlite3.connect(db)
    hechos, tot = conn.execute("""
        SELECT SUM(CASE WHEN s.dot_number IS NOT NULL THEN 1 ELSE 0 END),
               COUNT(*)
          FROM carriers c LEFT JOIN scrape_log s
            ON s.dot_number = c.dot_number
         WHERE c.phy_state = 'CA'""").fetchone()
    conn.close()

    if not tot:
        print("- CA: no hay carriers de CA en el censo")
        return

    print(f"- CA: {hechos:,} de {tot:,} ({hechos / tot * 100:.1f}%) — "
          f"pendientes {tot - hechos:,}")

    # El numero que decide si el plan aguanta: cuanto rindio ESTA pasada antes
    # de que el WAF cortara. Si cae pasada a pasada, la IP se esta quemando y no
    # hay cron que lo arregle: haria falta rotar IP.
    ck = BASE / "ca_scraper_checkpoint.json"
    if not ck.exists():
        return
    try:
        d = json.loads(ck.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"- (checkpoint ilegible: {e})")
        return

    print(f"- Esta pasada: {d.get('done', 0):,} carriers en "
          f"{d.get('elapsed_s', 0) / 60:.0f} min · "
          f"{d.get('rate_carriers_s', 0)} carriers/s")
    fb = d.get("waf_first_block_at_request")
    if fb:
        print(f"- WAF: primer bloqueo en la request #{fb:,} · "
              f"{d.get('waf_total_blocks', 0):,} bloqueos · "
              f"abortada: {d.get('aborted')}")
    elif d.get("aborted"):
        print("- Abortada sin bloqueo de WAF registrado")


if __name__ == "__main__":
    sys.exit(main())
