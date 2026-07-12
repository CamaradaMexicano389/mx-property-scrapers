#!/usr/bin/env python3
"""
PADIM Inmuebles24 Scraper — Playwright directo
Bypass Cloudflare Turnstile con anti-detección completa.
Scrapea listados + detalle individual de cada propiedad.
"""
import json, re, sys, hashlib, asyncio, random
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_DIR = Path("/home/padim/workspace/projects/PADIM-scraper/data")
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

ANTI_DETECT_JS = """
// Override navigator.webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// Override navigator.plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

// Override navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['es-MX', 'es', 'en-US', 'en']
});

// Override chrome.runtime
window.chrome = { runtime: {} };

// Override permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: 'denied' }) :
        originalQuery(parameters)
);

// Remove webdriver trace
for (const prop of Object.getOwnPropertyNames(navigator)) {
    if (prop.toLowerCase().includes('webdriver')) {
        Object.defineProperty(navigator, prop, { get: () => false });
    }
}
"""


def safe_int(v, default=0):
    if isinstance(v, (int, float)): return int(v)
    if isinstance(v, str):
        try: return int(float(v.replace(",","").replace("$","").strip()))
        except: pass
    return default


def i(v, d=0):
    return safe_int(v, d)


async def scrape_inmuebles24(max_props=10, headless=True):
    from playwright.async_api import async_playwright
    
    print(f"  🚀 Iniciando Playwright (headless={headless})...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--window-size=1920,1080',
            ]
        )
        
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1920, "height": 1080},
            locale='es-MX',
            timezone_id='America/Mexico_City',
            geolocation={"latitude": 19.4326, "longitude": -99.1332},
            permissions=["geolocation"],
            extra_http_headers={
                'Accept-Language': 'es-MX,es;q=0.9,en;q=0.8',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            }
        )
        
        await context.add_init_script(ANTI_DETECT_JS)
        
        page = await context.new_page()
        page.set_default_timeout(60000)
        
        # Ir a listado
        url = "https://www.inmuebles24.com/inmuebles-en-venta.html"
        print(f"  Navegando a: {url}")
        
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print(f"  ⚠️ Timeout en domcontentloaded: {e}")
            try:
                await page.goto(url, wait_until='load', timeout=60000)
            except Exception as e2:
                print(f"  ❌ Error cargando página: {e2}")
                await browser.close()
                return []
        
        # Esperar a que desaparezca Cloudflare
        print(f"  Esperando contenido... (URL: {page.url})")
        
        for attempt in range(15):
            await asyncio.sleep(2)
            title = await page.title()
            current_url = page.url
            
            if "just a moment" in title.lower() or "challenge" in title.lower():
                print(f"  ⏳ Intento {attempt+1}/15: Cloudflare presente... ({current_url[:80]})")
                continue
            
            # Verificar que hay contenido real
            body_text = await page.evaluate("document.body.innerText.substring(0, 500)")
            if "$" in body_text and len(body_text) > 200:
                print(f"  ✅ Contenido cargado! (intento {attempt+1})")
                break
            print(f"  ⏳ Esperando contenido real... ({len(body_text)} chars)")
        else:
            print(f"  ❌ Cloudflare no se resolvió después de 15 intentos")
            # Intentar un refresh
            try:
                await page.reload(wait_until='domcontentloaded')
                await asyncio.sleep(10)
            except:
                pass
            body = await page.evaluate("document.body.innerText.substring(0, 500)")
            print(f"  Post-reload: {body[:200]}")
            await browser.close()
            return []
        
        # Scroll humano
        for i in range(5):
            await page.evaluate(f"window.scrollBy(0, {random.randint(300, 700)})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        
        # Extraer cards de propiedades
        print(f"  Extrayendo cards...")
        cards = await page.evaluate("""
            () => {
                // Buscar cualquier contenedor que tenga precio
                const items = document.querySelectorAll(
                    'article, div[class*="card"], div[class*="property"], ' +
                    'div[class*="listing"], div[class*="item"], div[data-index]'
                );
                
                const results = [];
                const seen = new Set();
                
                for (const el of items) {
                    const text = el.innerText || '';
                    // Debe tener $ y un número de 5+ dígitos
                    const priceMatch = text.match(/\\$\\s*[0-9,]{5,}/);
                    if (!priceMatch) continue;
                    
                    const links = el.querySelectorAll('a');
                    const url = Array.from(links).find(a => {
                        const h = a.href || '';
                        return h.includes('propiedades') || h.includes('inmueble') || h.includes('clasificado');
                    })?.href || (links[0]?.href || '');
                    
                    if (!url || seen.has(url)) continue;
                    seen.add(url);
                    
                    const imgs = el.querySelectorAll('img');
                    const imgSrc = Array.from(imgs).find(img => {
                        const src = img.src || '';
                        return src.startsWith('http') && !src.includes('logo') && !src.includes('icon');
                    })?.src || '';
                    
                    results.push({
                        url: url,
                        title: (el.querySelector('h2, h3, [class*="title"], [class*="name"]') || {}).innerText?.trim?.() || '',
                        price: priceMatch[0] || '',
                        text: text.substring(0, 500),
                        image: imgSrc,
                    });
                    
                    if (results.length >= 20) return results;
                }
                return results;
            }
        """)
        
        print(f"  ✅ {len(cards)} cards encontradas")
        
        if not cards:
            # Fallback: dump HTML para debug
            html = await page.content()
            print(f"  ⚠️ HTML dump (primeros 2000 chars):")
            print(f"    {html[:2000]}")
            await browser.close()
            return []
        
        props = []
        for idx, card in enumerate(cards[:max_props]):
            print(f"  [{idx+1}/{min(len(cards), max_props)}] {card['url'][:80]}...")
            
            try:
                await page.goto(card['url'], wait_until='domcontentloaded', timeout=30000)
                await asyncio.sleep(random.uniform(0.5, 1.5))
                
                # Extraer detalle
                detail = await page.evaluate("""
                    () => {
                        const get = (sel) => {
                            const el = document.querySelector(sel);
                            return el ? el.innerText.trim() : '';
                        };
                        const getAll = (sel) => {
                            return Array.from(document.querySelectorAll(sel)).map(e => e.innerText.trim());
                        };
                        const getAttr = (sel, attr) => {
                            const el = document.querySelector(sel);
                            return el ? el.getAttribute(attr) || '' : '';
                        };
                        
                        return {
                            description: get('[class*="description"], [class*="descripcion"], [itemprop="description"], p'),
                            features: getAll('[class*="feature"] li, [class*="caracteristic"] li, ul.list li'),
                            price_detail: get('[class*="price"], [class*="precio"]'),
                            m2_text: get('[class*="area"], [class*="surface"], [class*="metros"], [class*="superficie"]'),
                            location: get('[class*="location"], [class*="ubicacion"], [class*="address"], [itemprop="address"]'),
                            images: Array.from(document.querySelectorAll('img[src*="http"]'))
                                .filter(i => !i.src.includes('logo') && !i.src.includes('icon'))
                                .slice(0, 5)
                                .map(i => i.src),
                            full_text: document.body.innerText.substring(0, 3000),
                        };
                    }
                """)
                
                full_text = detail.get('full_text', '')
                text = f"{card.get('title','')} {full_text}"
                
                # Precio
                price = 0
                pm = re.search(r'\\$\\s*([0-9,]{5,})', full_text)
                if pm: price = safe_int(pm.group(1))
                if not price:
                    pm2 = re.search(r'([1-9][0-9]{4,6}(?:[\\.,][0-9]{2,3})?)', full_text)
                    if pm2: price = safe_int(pm2.group(1))
                
                # Recámaras
                rec = 0
                rm = re.search(r'(\\d+)\\s*(?:recámaras?|recamaras?|habitaciones?|rec\\.)', text, re.I)
                if rm: rec = int(rm.group(1))
                
                # Baños
                ban = 0
                bm = re.search(r'(\\d+)\\s*(?:baños?|banos?)', text, re.I)
                if bm: ban = int(bm.group(1))
                
                # Metros
                m2 = 0
                mm = re.search(r'(\\d+[\\d,.]*)\\s*(?:m²|m2|metros?\\s*(?:construidos?|cuadrados?))', text, re.I)
                if mm: m2 = safe_int(mm.group(1))
                
                # Tipo
                tipo = "departamento"
                if re.search(r'\\bcasa\\b', text[:500], re.I): tipo = "casa"
                if re.search(r'\\bterreno\\b', text[:500], re.I): tipo = "terreno"
                if re.search(r'\\blocal\\b', text[:500], re.I): tipo = "local"
                if re.search(r'\\boficina\\b', text[:500], re.I): tipo = "oficina"
                
                # Ubicación
                ubicacion = detail.get('location', '') or card.get('text', '')
                colonia = ''
                cm = re.search(r'Col(?:onia)?[.:]?\\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑa-záéíóúñ\\s]+)', ubicacion)
                if cm: colonia = cm.group(1).strip()[:80]
                
                prop = {
                    "source": "inmuebles24",
                    "portal": "inmuebles24",
                    "titulo": (card.get('title', '') or full_text[:100]).strip()[:200],
                    "precio": price,
                    "moneda": "MXN",
                    "tipo_inmueble": tipo,
                    "tipo_operacion": "venta",
                    "recamaras": rec,
                    "banos": ban,
                    "metros_cuadrados": m2,
                    "colonia": colonia,
                    "ciudad": "Ciudad de México",
                    "estado": "CDMX",
                    "direccion": ubicacion[:200],
                    "url": card.get('url', ''),
                    "fotos": detail.get('images', [])[:5],
                    "descripcion": detail.get('description', '')[:500],
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
                
                raw = f"{prop['titulo']}:{price}:{rec}:{colonia}"
                prop["fingerprint"] = hashlib.sha256(raw.lower().strip().encode()).hexdigest()
                props.append(prop)
                
                print(f"    ✓ ${price:,} | {rec} rec | {colonia[:30]}")
                
            except Exception as e:
                print(f"    ⚠️ Error en detalle: {e}")
                continue
        
        await browser.close()
        print(f"  ✅ {len(props)}/{len(cards[:max_props])} propiedades")
        return props


if __name__ == "__main__":
    import sys
    headless = '--headful' not in sys.argv
    max_p = 10
    
    for arg in sys.argv[1:]:
        if arg.startswith('--max='):
            max_p = int(arg.split('=')[1])
    
    props = asyncio.run(scrape_inmuebles24(max_props=max_p, headless=headless))
    
    if props:
        output_path = OUTPUT_DIR / f"inmuebles24_{TIMESTAMP}.json"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(props, f, indent=2, ensure_ascii=False)
        print(f"\n  📁 {output_path} ({len(props)} props)")
        
        # Resumen
        total_price = sum(p['precio'] for p in props)
        avg_price = total_price / len(props) if props else 0
        print(f"  💰 Precio promedio: ${avg_price:,.0f}")
        print(f"  🏠 Tipos: {', '.join(set(p['tipo_inmueble'] for p in props))}")
        for p in props[:5]:
            print(f"    {p['titulo'][:55]} | ${p['precio']:,} | {p['colonia'][:20]} | {p['recamaras']}rec")
    else:
        print("\n  ❌ No se obtuvieron propiedades")
        sys.exit(1)
