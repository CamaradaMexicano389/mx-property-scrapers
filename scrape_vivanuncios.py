#!/usr/bin/env python3
"""
PADIM Daily Vivanuncios Scraper — curl_cffi directo + PRELOADED_STATE
Bypass Cloudflare. Extrae 30 propiedades por ejecución.
"""
import json, re, sys, hashlib
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_DIR = Path("/home/padim/workspace/projects/PADIM-scraper/data")
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

TYPE_MAP = {
    "Casa": "casa", "Casas": "casa",
    "Departamento": "departamento", "Departamentos": "departamento",
    "Terreno": "terreno", "Terrenos": "terreno",
    "Local": "local", "Locales comerciales": "local",
    "Oficina": "oficina", "Oficinas": "oficina",
    "Bodega": "bodega",
    "Nave industrial": "nave_industrial",
    "Edificio": "edificio",
    "Desarrollo": "otro",
}

def s(v, d=""):
    """String seguro."""
    if isinstance(v, str): return v
    if isinstance(v, dict): return s(v.get("name", ""))
    return str(v) if v is not None else d

def i(v, d=0):
    """Int seguro."""
    if isinstance(v, (int, float)): return int(v)
    if isinstance(v, str):
        try: return int(float(v.replace(",","").replace("$","")))
        except: pass
    return d

def get_loc(d, *keys):
    """Navega dict y retorna .name."""
    for k in keys:
        if isinstance(d, dict) and k in d: d = d[k]
        else: return ""
    return s(d.get("name")) if isinstance(d, dict) else ""

def scrape_vivanuncios(max_props=30):
    from curl_cffi import requests as curl_requests

    url = "https://www.vivanuncios.com.mx/s-venta-inmuebles/v1c1097p1"
    print(f"  Fetching: {url}")
    r = curl_requests.get(url, impersonate="chrome110", timeout=30)
    if r.status_code != 200:
        print(f"  ❌ Status {r.status_code}")
        return []
    print(f"  ✅ Status 200, {len(r.text):,} chars")

    m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.+?\});', r.text, re.DOTALL)
    if not m:
        print("  ❌ No PRELOADED_STATE")
        return []
    
    data = json.loads(m.group(1))
    postings = data.get("listStore", {}).get("listPostings", [])
    print(f"  ✅ {len(postings)} propiedades")

    props = []
    for item in postings[:max_props]:
        try:
            # Precio
            pts = item.get("priceOperationTypes", [])
            pt = pts[0] if isinstance(pts, list) and pts else {}
            prices = pt.get("prices", []) if isinstance(pt, dict) else []
            amount = i(prices[0].get("amount")) if prices else 0

            # Ubicación
            pl = item.get("postingLocation", {}) or {}
            colonia = get_loc(pl, "location")
            delegacion = get_loc(pl, "location", "parent")
            ciudad = get_loc(pl, "location", "parent", "parent")
            direccion = s(pl.get("address", {}).get("name")) if isinstance(pl.get("address"), dict) else ""

            # Tipo
            rt = item.get("realEstateType", {})
            type_name = rt.get("name", "") if isinstance(rt, dict) else s(rt)
            tipo = TYPE_MAP.get(type_name, "departamento")

            # Fotos
            vp = item.get("visiblePictures", {}) or {}
            pics = vp.get("pictures", []) if isinstance(vp, dict) else []
            fotos = []
            for p in pics[:5]:
                if isinstance(p, dict) and p.get("url"):
                    fotos.append(p["url"])
                elif isinstance(p, str):
                    fotos.append(p)

            # Features desde descriptionNormalized o features
            desc = s(item.get("descriptionNormalized", ""))
            recamaras = 0
            banos = 0
            m2_c = 0
            m2_t = 0
            
            m = re.search(r'(\d+)\s*(?:recámaras?|recamaras?|habitaciones?|rec\.)', desc, re.I)
            if m: recamaras = int(m.group(1))
            m = re.search(r'(\d+)\s*(?:baños?|banos?)', desc, re.I)
            if m: banos = int(m.group(1))
            m = re.search(r'(\d+[\d,.]*)\s*(?:m²|m2|metros?\s*(?:construidos?|cuadrados?))', desc, re.I)
            if m: m2_c = int(float(m.group(1).replace(",","")))
            m = re.search(r'(\d+[\d,.]*)\s*(?:metros?\s*terreno)', desc, re.I)
            if m: m2_t = int(float(m.group(1).replace(",","")))

            titulo = s(item.get("generatedTitle", item.get("title", "")))

            prop = {
                "source": "vivanuncios",
                "portal": "vivanuncios",
                "titulo": titulo[:200],
                "precio": amount,
                "moneda": "MXN",
                "tipo_inmueble": tipo,
                "tipo_operacion": "venta",
                "recamaras": recamaras,
                "banos": banos,
                "metros_cuadrados": m2_c,
                "metros_terreno": m2_t,
                "direccion": direccion[:200],
                "colonia": colonia[:100],
                "delegacion": delegacion[:100],
                "ciudad": ciudad[:100],
                "estado": "CDMX",
                "url": f"https://www.vivanuncios.com.mx{item.get('listingUrl','')}",
                "fotos": fotos,
                "fecha_publicacion": s(item.get("modified_date")),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }

            raw = f"{titulo}:{amount}:{recamaras}:{colonia}"
            prop["fingerprint"] = hashlib.sha256(raw.lower().strip().encode()).hexdigest()
            props.append(prop)
        except Exception as e:
            print(f"  ⚠️  Error: {e}")
            continue

    print(f"  ✅ {len(props)}/{len(postings[:max_props])} parseadas")
    return props


if __name__ == "__main__":
    props = scrape_vivanuncios(max_props=30)
    if props:
        output_path = OUTPUT_DIR / f"vivanuncios_{TIMESTAMP}.json"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(props, f, indent=2, ensure_ascii=False)
        print(f"\n  📁 {output_path} ({len(props)} props)")
        for p in props[:5]:
            print(f"    {p['titulo'][:60]} | ${p['precio']:,} | {p['colonia']} | {p['recamaras']} rec")
    else:
        print("\n  ⚠️  Sin resultados")
        sys.exit(1)
