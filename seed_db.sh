#!/usr/bin/env bash
# ============================================================================
# movik/seed_db.sh — sube movik.db como semilla para GitHub Actions
# ============================================================================
# El problema: movik.db pesa 168 MB, no puede vivir en el repo, y los workflows
# lo pasan de un run al siguiente como artifact. Pero el PRIMER run no tiene run
# anterior del cual restaurarlo, y los artifacts vencen a los 90 días.
#
# La solución más simple: un release fijo llamado "db-seed" con movik.db como
# asset. Los releases no vencen y aguantan hasta 2 GB por archivo. Los workflows
# lo bajan solo cuando no encuentran artifact previo.
#
# Uso:
#   bash seed_db.sh          # crea o actualiza el release db-seed
#
# Cuándo correrlo:
#   - Una vez, antes del primer run de los workflows.
#   - Cada vez que quieras refrescar la semilla desde tu movik.db local
#     (por ejemplo tras recargar el censo con 01_census_incremental.py).
# ============================================================================
set -euo pipefail

REPO="The-Magic-Hack/The-Magic-Hack-movik-pipeline"
TAG="db-seed"
DB="movik.db"

cd "$(dirname "$0")"

if [ ! -f "$DB" ]; then
  echo "❌ No existe $DB en $(pwd)"
  exit 1
fi

SIZE=$(du -h "$DB" | cut -f1)
echo "📦 Subiendo $DB ($SIZE) al release $TAG de $REPO"

# El release es un contenedor fijo: se crea una vez y después solo se
# reemplaza el asset con --clobber.
if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release create "$TAG" --repo "$REPO" \
    --title "movik.db — semilla para GitHub Actions" \
    --notes "Copia de movik.db con el censo FL+CA+NC cargado. Los workflows la bajan cuando no hay artifact de un run anterior. No es un release de software." \
    --latest=false
fi

gh release upload "$TAG" "$DB" --repo "$REPO" --clobber

echo "✅ Listo. Verifica con: gh release view $TAG --repo $REPO"
