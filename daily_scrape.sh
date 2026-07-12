#!/usr/bin/env bash
# PADIM Daily Scrape v2.0 — Pipeline consolidado
# 1) Scrapea todos los portales
# 2) Consolida a propiedades.jsonl (schema RESO unificado)
# 3) Sincroniza a base de datos
# 4) Actualiza status.json
set -euo pipefail

REPO_DIR="/home/padim/workspace/projects/PADIM-scraper"
VENV="$REPO_DIR/.venv-padim"
OUTPUT_DIR="$REPO_DIR/data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
START_TS=$(date +%s)

cd "$REPO_DIR"
source "$VENV/bin/activate"
mkdir -p "$OUTPUT_DIR"

echo "═══════════════════════════════════════════════"
echo " PADIM Daily Scrape v2.0"
echo " $(date)"
echo "═══════════════════════════════════════════════"
echo ""

# ── 1. Lamudi (funcional, DynamicFetcher) ──
echo "─── 1/5 Lamudi ───"
python3 -m padim.cli.main scrape lamudi \
    --colony "ciudad-de-mexico" \
    --output "$OUTPUT_DIR/lamudi_$TIMESTAMP.json" 2>&1 || echo "⚠️  Lamudi falló"
echo ""

# ── 2. Vivanuncios (curl_cffi directo, bypass Cloudflare) ──
echo "─── 2/5 Vivanuncios ───"
python3 scripts/scrape_vivanuncios.py 2>&1 || echo "⚠️  Vivanuncios falló"
echo ""

# ── 3. Inmuebles24 (StealthyFetcher, timeout 180s) ──
echo "─── 3/5 Inmuebles24 ───"
timeout 180 python3 -m padim.cli.main scrape inmuebles24 \
    --colony "ciudad-de-mexico" \
    --output "$OUTPUT_DIR/inmuebles24_$TIMESTAMP.json" 2>&1 || echo "⚠️  Inmuebles24 falló (Cloudflare)"
echo ""

# ── 4. Propiedades.com (curl_cffi universal) ──
echo "─── 4/5 Propiedades.com ───"
timeout 60 python3 -m padim.cli.main scrape propiedades \
    --colony "ciudad-de-mexico" \
    --output "$OUTPUT_DIR/propiedades_$TIMESTAMP.json" 2>&1 || echo "⚠️  Propiedades.com falló"
echo ""

# ── 5. EasyBroker (universal) ──
echo "─── 5/5 EasyBroker ───"
timeout 60 python3 -m padim.cli.main scrape easybroker \
    --colony "ciudad-de-mexico" \
    --output "$OUTPUT_DIR/easybroker_$TIMESTAMP.json" 2>&1 || echo "⚠️  EasyBroker falló"
echo ""

# ── Resumen de scrape ──
echo "─── Resumen de scraping ───"
TOTAL=0
for f in "$OUTPUT_DIR"/*"$TIMESTAMP"*.json; do
    if [ -f "$f" ]; then
        COUNT=$(python3 -c "import json; data=json.load(open('$f')); print(len(data))" 2>/dev/null || echo "error")
        echo "  $(basename $f): $COUNT propiedades"
        if [ "$COUNT" != "error" ]; then TOTAL=$((TOTAL + COUNT)); fi
    fi
done
echo "  TOTAL scrapeado: $TOTAL propiedades"
echo ""

# ── Consolidar a propiedades.jsonl + DB ──
echo "─── Consolidando a propiedades.jsonl + DB ───"
python3 scripts/consolidate_to_jsonl.py 2>&1 || echo "⚠️  Consolidación falló"
echo ""

# ── Generar status.json ──
echo "─── Actualizando status.json ───"
if [ -f "$REPO_DIR/site/status.json" ]; then
    PROPS_COUNT=$(wc -l < "$OUTPUT_DIR/propiedades.jsonl" 2>/dev/null || echo 0)
    END_TS=$(date +%s)
    DURATION=$((END_TS - START_TS))
    
    python3 -c "
import json, sys
path = '$REPO_DIR/site/status.json'
with open(path) as f:
    s = json.load(f)
s['last_scrape'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
s['duration_seconds'] = $DURATION
s['total_properties'] = $PROPS_COUNT
s['sources_active'] = 5
s['status'] = 'operational'
with open(path, 'w') as f:
    json.dump(s, f, indent=2)
print('   ✅ status.json actualizado')
" 2>&1 || echo "⚠️  status.json falló"
fi

# ── COMPLETADO ──
END_TS=$(date +%s)
DURATION=$((END_TS - START_TS))
MIN=$((DURATION / 60))
SEC=$((DURATION % 60))
echo ""
echo "═══════════════════════════════════════════════"
echo " COMPLETADO: $(date) (${MIN}m ${SEC}s)"
echo " Propiedades en JSONL: $(wc -l < $OUTPUT_DIR/propiedades.jsonl 2>/dev/null || echo 0)"
echo "═══════════════════════════════════════════════"