#!/usr/bin/env python3
"""
PADIM Scraper — Conectores inmobiliarios MX
Scrapling 0.4.9 + StealthyFetcher/DynamicFetcher

Convertido a módulo: exporta clase PADIMScraper.
"""
import hashlib, json, logging, os, sys, time, re, yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

try:
    import yaml
    from scrapling import StealthyFetcher, DynamicFetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("padim")

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.yaml"


def load_config() -> dict:
    """Carga config desde YAML con fallback a defaults."""
    if not HAS_SCRAPLING:
        log.warning("Scrapling/yaml no instalado, usando config por defecto")
        return _default_config()
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _default_config() -> dict:
    """Config por defecto cuando YAML no está disponible."""
    return {
        "settings": {
            "max_properties_per_portal": 3,
            "max_detail_retries": 1,
            "request_delay": 0.5,
            "output_file": "data/propiedades.jsonl",
        },
        "portals": {
            "lamudi": {
                "enabled": True,
                "fetcher": "dynamic",
                "solve_cloudflare": False,
                "base_url": "https://www.lamudi.com.mx",
                "listing_url": "https://www.lamudi.com.mx/for-sale/",
                "detail_pattern": "/detalle/",
            }
        }
    }


def fingerprint(prop: dict) -> str:
    """SHA256 fingerprint para dedup intra-portal (sin precio=0)."""
    precio = prop.get('precio', 0)
    precio_str = f":{precio}" if precio > 0 else ""
    raw = f"{prop.get('titulo','')}{precio_str}:{prop.get('metros_cuadrados',0)}:{prop.get('recamaras',0)}:{prop.get('direccion','')}"
    raw = raw.lower().strip()
    return hashlib.sha256(raw.encode()).hexdigest()


# ── SISTEMA DE CALIDAD ──────────────────────────────────────

ZONAS_USD = [
    "cancun", "cabo san lucas", "los cabos", "puerto vallarta",
    "san miguel de allende", "playa del carmen", "tulum",
    "isla mujeres", "cozumel", "la paz bcs", "los barriles",
    "sayulita", "punta mita", "careyes", "zihuatanejo",
    "huatulco", "puerto escondido",
]

def detectar_moneda(prop: dict) -> tuple:
    """Detecta moneda (USD/MXN) y devuelve (moneda, confianza, precio_corregido)."""
    texto = f"{prop.get('titulo','')} {prop.get('descripcion','')}"
    ubicacion = f"{prop.get('colonia','')} {prop.get('ciudad','')} {prop.get('estado','')}"
    precio = prop.get("precio", 0)
    
    senales = []
    
    # Señal 1: Símbolo explícito (alta confianza)
    if re.search(r'us\s*\$|usd|dls|d[óo]lar(?:es)?\b', texto, re.I):
        senales.append(("USD", 0.95))
    if re.search(r'mxn|mx\s*\$|pesos?\b', texto, re.I):
        senales.append(("MXN", 0.95))
    
    # Señal 2: Precio absoluto
    if precio > 50_000_000:
        senales.append(("MXN", 0.7))
    elif precio > 10_000_000:
        senales.append(("MXN", 0.6))
    elif 0 < precio < 50_000:
        senales.append(("USD", 0.4))
    
    # Señal 3: Ubicación turística
    if any(z in ubicacion.lower() for z in ZONAS_USD):
        senales.append(("USD", 0.35))
    
    # Señal 4: Formato numérico con punto decimal
    precio_str = str(precio)
    if "." in precio_str[-6:] or re.search(r'[0-9]+\.[0-9]{2}', precio_str):
        senales.append(("USD", 0.2))
    
    # Votación ponderada
    score_usd = sum(s[1] for s in senales if s[0] == "USD")
    score_mxn = sum(s[1] for s in senales if s[0] == "MXN")
    
    if score_usd > 0.5 and score_usd > score_mxn:
        confianza = min(score_usd / (score_usd + score_mxn + 0.001), 0.99)
        return "USD", round(confianza, 2), precio
    return "MXN", round(0.5 + score_mxn / (score_usd + score_mxn + 0.001), 2), precio


TIPO_OPERACION_KEYWORDS = {
    "renta": r"renta|arriendo|alquiler|lease|mensual|/mes|por mes",
    "venta": r"venta|compra|buy|sell|adquirir|preventa",
    "subasta": r"subasta|remate|judicial|bankrupcy|foreclosure",
}


def detectar_operacion(prop: dict) -> tuple:
    """Detecta tipo de operación (venta/renta/subasta)."""
    texto = f"{prop.get('titulo','')} {prop.get('descripcion','')}"
    
    for operacion, patron in TIPO_OPERACION_KEYWORDS.items():
        if re.search(patron, texto, re.I):
            confianza = 0.85 if operacion == prop.get("tipo_operacion", "") else 0.7
            return operacion, confianza
    
    return prop.get("tipo_operacion", "venta"), 0.5


def validar_calidad(prop: dict) -> dict:
    """Ejecuta todo el pipeline de validación y devuelve score + flags."""
    flags = []
    precio = prop.get("precio", 0)
    
    # ── Nivel 1: Formato ──
    if precio <= 0:
        flags.append("precio_invalido")
    if not (0 <= prop.get("recamaras", 0) <= 50):
        flags.append("recamaras_invalidas")
    if not (0 <= prop.get("banos", 0) <= 50):
        flags.append("banos_invalidos")
    if not (0 <= prop.get("metros_cuadrados", 0) <= 100_000):
        flags.append("metros_invalidos")
    
    # ── Nivel 2: Semántica ──
    moneda, conf_moneda, precio_corregido = detectar_moneda(prop)
    operacion, conf_operacion = detectar_operacion(prop)
    
    if moneda != "MXN":
        flags.append("moneda_usd")
        prop["precio_mxn"] = round(precio_corregido * 18.5, 2)
    else:
        prop["precio_mxn"] = precio
    
    if operacion != prop.get("tipo_operacion", ""):
        flags.append(f"operacion_inconsistente:{operacion}")
        prop["tipo_operacion"] = operacion
    
    # Detectar precio mensual vs total
    texto = f"{prop.get('titulo','')} {prop.get('descripcion','')}"
    if re.search(r'mensual|/mes|por mes', texto, re.I) and precio < 1_000_000:
        if prop.get("tipo_operacion") != "renta":
            flags.append("posible_renta_mensual")
    
    # Detectar rango 'desde'
    if re.search(r'desde\s*\$', texto, re.I):
        flags.append("precio_desde")
    
    # Detectar mantenimiento incluido
    if re.search(r'incluye\s*(mantenimiento|condominio)', texto, re.I):
        flags.append("mantenimiento_incluido")
    
    # ── Detector de fraude ──
    fraud_flags = detectar_posible_fraude(prop)
    flags.extend(fraud_flags)
    
    # ── Score de confianza ──
    penalizaciones = {
        "precio_invalido": 0.5,
        "moneda_usd": 0.25,
        "operacion_inconsistente": 0.2,
        "posible_renta_mensual": 0.15,
        "precio_desde": 0.1,
        "mantenimiento_incluido": 0.05,
        "recamaras_invalidas": 0.2,
        "banos_invalidos": 0.2,
        "metros_invalidos": 0.15,
        "precio_muy_bajo": 0.4,
        "texto_presion": 0.1,
        "fotos_insuficientes": 0.1,
    }
    
    score = 1.0
    for flag in flags:
        flag_key = flag.split(":")[0]
        score -= penalizaciones.get(flag_key, 0.1)
    
    score = max(0.0, min(1.0, score))
    score = round(score, 2)
    
    prop["calidad"] = {
        "score": score,
        "flags": flags,
        "moneda_detectada": moneda,
        "confianza_moneda": conf_moneda,
    }
    
    return prop


# ── EXTRACTOR MEJORADO (JSON-LD + data-atributos) ───────────

def extraer_precio_jsonld(html: str) -> int | None:
    """Busca precio en JSON-LD (estructurado) y data-atributos."""
    # JSON-LD
    jsonlds = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for j in jsonlds:
        try:
            data = json.loads(j)
            # Puede ser un objeto o un @graph
            items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for item in items:
                if isinstance(item, dict):
                    offers = item.get("offers", {})
                    if isinstance(offers, dict):
                        price = offers.get("price", 0)
                        if price and float(price) > 10000:
                            return int(float(price))
                    # Precio directo
                    price = item.get("price", 0)
                    if price and float(price) > 10000:
                        return int(float(price))
        except (json.JSONDecodeError, ValueError):
            pass
    
    # Data-atributos (data-price="4500000")
    data_prices = re.findall(r'data-price\s*=\s*["\']?([0-9]+)["\']?', html, re.I)
    for p in data_prices:
        val = int(p)
        if val > 10000:
            return val
    
    return None


def extraer_ubicacion_jsonld(html: str) -> dict:
    """Extrae ubicacion (direccion, ciudad, estado, lat, lng) de JSON-LD."""
    result = {}
    jsonlds = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for j in jsonlds:
        try:
            data = json.loads(j) if isinstance(j, str) else j
            items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Address
                addr = item.get("address", {}) or {}
                if isinstance(addr, dict):
                    if addr.get("streetAddress"):
                        result["direccion"] = addr["streetAddress"]
                    if addr.get("addressLocality"):
                        result["ciudad"] = addr["addressLocality"]
                    if addr.get("addressRegion"):
                        result["estado"] = addr["addressRegion"]
                # Geo
                geo = item.get("geo", {}) or {}
                if isinstance(geo, dict):
                    if geo.get("latitude"):
                        result["latitud"] = float(geo["latitude"])
                    if geo.get("longitude"):
                        result["longitud"] = float(geo["longitude"])
                # Name as title fallback
                if item.get("name") and not result.get("titulo"):
                    result["titulo"] = item["name"]
                # Description
                if item.get("description") and not result.get("descripcion"):
                    result["descripcion"] = item["description"]
                # Image
                if item.get("image"):
                    img = item["image"]
                    if isinstance(img, str):
                        result.setdefault("fotos", []).append(img)
                    elif isinstance(img, list):
                        result.setdefault("fotos", []).extend(img)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return result


# ── EXTRACTOR IA (Ring-2.6-1T) ────────────────────────────

OPENROUTER_API_KEY = None
RING_MODEL = "inclusionai/ling-2.6-flash"
RING_FALLBACK = "inclusionai/ring-2.6-1t"

def _get_ring_key() -> str:
    global OPENROUTER_API_KEY
    if OPENROUTER_API_KEY:
        return OPENROUTER_API_KEY
    # Intentar del entorno
    raw = os.environ.get("OPENROUTER_API_KEY", "")
    if raw:
        # Limpiar keys concatenadas por censura de Hermes
        keys = raw.replace("[", "|").split("|")
        for k in keys:
            k = k.replace("]", "").strip()
            if k.startswith("sk-or-") and len(k) > 40:
                OPENROUTER_API_KEY = k
                return k
    return ""


def extraer_con_ia(html: str) -> dict:
    """Usa Ling-2.6-flash para extraer datos. Fallback a Ring-2.6-1T si falla."""
    key = _get_ring_key()
    if not key:
        return {}
    
    modelos = [RING_MODEL, RING_FALLBACK]

    for modelo in modelos:
        try:
            import httpx
            r = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": modelo,
                    "messages": [
                        {"role": "system", "content": "Eres un extractor de datos inmobiliarios de Mexico. Responde SOLO con JSON valido."},
                        {"role": "user", "content": f"Del siguiente HTML de una propiedad inmobiliaria mexicana extrae SOLO estos campos en JSON: precio (numero entero en MXN), recamaras (numero), banos (numero), metros_cuadrados (numero), direccion (texto), colonia (texto), ciudad (texto), tipo_inmueble (casa/departamento/terreno/local), tipo_operacion (venta/renta). Si un campo no se encuentra, pon null.\n\nHTML:\n{html[:4000]}"}
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 500
                },
                timeout=30
            )
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                resultado = json.loads(content)
                # Si Ling devolvio precio valido, usarlo. Si no, probar Ring
                if modelo == RING_MODEL and resultado.get("precio"):
                    return resultado
                elif modelo == RING_FALLBACK and resultado.get("precio"):
                    return resultado
            # Si Ling no dio precio, probar con Ring
            if modelo == RING_MODEL:
                continue
        except Exception:
            if modelo == RING_MODEL:
                continue
            pass
    return {}


# ── DETECTOR DE FRAUDE ──────────────────────────────────────
# Precio promedio por zona (se poblará con datos históricos)
PRECIO_PROMEDIO_POR_ZONA = {}  # {"colonia_ciudad_estado": precio_promedio}

def detectar_posible_fraude(prop: dict) -> list:
    """Detecta senales de posible fraude."""
    fraud_flags = []
    precio = prop.get("precio", 0)
    fotos = prop.get("fotos", [])
    
    # Senal 1: Precio sospechosamente bajo vs el promedio de la zona
    clave_zona = f"{prop.get('colonia','').lower()}_{prop.get('ciudad','').lower()}_{prop.get('estado','').lower()}"
    if clave_zona in PRECIO_PROMEDIO_POR_ZONA:
        promedio = PRECIO_PROMEDIO_POR_ZONA[clave_zona]
        if 0 < precio < promedio * 0.5:
            fraud_flags.append("precio_muy_bajo:posible_fraude")
    
    # Senal 2: Mucho texto de presion/urgencia
    texto = f"{prop.get('titulo','')} {prop.get('descripcion','')}".lower()
    if re.search(r'(urgente|solo hoy|ultima oportunidad|deposito|apartado|transferencia)', texto):
        fraud_flags.append("texto_presion")
    
    # Senal 3: Muy pocas fotos
    if len(fotos) <= 1:
        fraud_flags.append("fotos_insuficientes")
    
    return fraud_flags


# ── CONECTOR A BASE DE DATOS ─────────────────────────────────

import os as _db_os

DB_CONFIG = {
    "host": os.getenv("PADIM_DB_HOST", "localhost"),
    "port": int(os.getenv("PADIM_DB_PORT", "5432")),
    "user": os.getenv("PADIM_DB_USER", "padim"),
    "password": os.getenv("PADIM_DB_PASSWORD", ""),
    "dbname": os.getenv("PADIM_DB_NAME", "padim"),
}

def get_db_connection():
    """Crea conexion a PostgreSQL para guardar datos scrapeados."""
    try:
        import psycopg2
        return psycopg2.connect(**DB_CONFIG)
    except ImportError:
        log.warning("psycopg2 no instalado, solo guardando en JSONL")
        return None
    except Exception as e:
        log.warning(f"Error conectando a DB: {e}")
        return None


def save_to_padim_db(prop: dict) -> bool:
    """Guarda propiedad en PostgreSQL de PADIM."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        from uuid import uuid4
        
        source = prop.get("portal", "padim_scraper")
        source_id = prop.get("fingerprint", str(uuid4()))
        price = prop.get("precio_mxn") or prop.get("precio", 0)
        m2 = prop.get("metros_cuadrados", 0)
        
        cur.execute("""
            INSERT INTO properties 
                (id, source, source_id, source_url, title, description,
                 property_type, business_type, price, currency,
                 m2_constructed, bedrooms, bathrooms,
                 address, colony, state, images, scraped_at)
            VALUES 
                (%s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s,
                 %s, %s, %s,
                 %s, %s, %s, %s::json, NOW())
            ON CONFLICT (source, source_id) 
            DO UPDATE SET 
                price = EXCLUDED.price,
                last_updated = NOW(),
                title = EXCLUDED.title,
                description = EXCLUDED.description
        """, (
            str(uuid4()), source, source_id,
            prop.get("url", ""), prop.get("titulo", ""), prop.get("descripcion", ""),
            prop.get("tipo_inmueble", ""), prop.get("tipo_operacion", "venta"),
            float(price) if price else 0, prop.get("moneda", "MXN"),
            float(m2) if m2 else None,
            int(prop.get("recamaras", 0)) or None,
            float(prop.get("banos", 0)) or None,
            prop.get("direccion", ""), prop.get("colonia", ""),
            prop.get("estado", ""),
            json.dumps(prop.get("fotos", []))
        ))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        log.warning(f"Error guardando en DB: {e}")
        return False
    finally:
        try:
            conn.close()
        except:
            pass


def normalize_property(data: dict, portal: str) -> dict:
    """Normaliza datos de cualquier portal a esquema unificado."""
    prop = {
        "portal": portal,
        "url": data.get("url", ""),
        "titulo": data.get("titulo", ""),
        "precio": data.get("precio", 0),
        "moneda": "MXN",
        "tipo_operacion": data.get("tipo_operacion", "venta"),
        "tipo_inmueble": data.get("tipo_inmueble", ""),
        "recamaras": data.get("recamaras", 0),
        "banos": data.get("banos", 0),
        "metros_cuadrados": data.get("metros_cuadrados", 0),
        "metros_terreno": data.get("metros_terreno", 0),
        "direccion": data.get("direccion", ""),
        "colonia": data.get("colonia", ""),
        "ciudad": data.get("ciudad", ""),
        "estado": data.get("estado", ""),
        "descripcion": data.get("descripcion", ""),
        "fotos": data.get("fotos", []),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    prop["fingerprint"] = fingerprint(prop)
    prop = validar_calidad(prop)
    return prop


def save_property(prop: dict, seen: set) -> bool:
    """Guarda si no está duplicado y tiene calidad suficiente."""
    score = prop.get("calidad", {}).get("score", 0)
    # No guardar si calidad es muy baja (score < 0.3)
    if score < 0.3:
        log.warning(f"  ✗ Rechazada (score:{score}): {prop['titulo'][:50]}")
        return False
    
    fp = prop["fingerprint"]
    if fp in seen:
        log.info(f"  ↺ Duplicado: {prop['titulo'][:50]}")
        return False
    seen.add(fp)
    
    # Guardar en JSONL (siempre)
    filepath = DATA_DIR / "propiedades.jsonl"
    with open(filepath, "a") as f:
        f.write(json.dumps(prop, ensure_ascii=False) + "\n")
    
    # Guardar en PADIM DB
    db_ok = save_to_padim_db(prop)
    if db_ok:
        log.info(f"  ✓ Sincronizado a DB")
    return True


# ── CONECTORES ──────────────────────────────────────────────

class Inmuebles24Connector:
    def __init__(self, config):
        self.cfg = config

    def extract_links(self, page) -> list:
        # Inmuebles24 usa cards con enlaces a /propiedades/clasificado/
        links = page.css('a::attr(href)')
        detail_links = []
        for l in links:
            href = str(l)
            # Los detalles de propiedades tienen /propiedades/clasificado/ o contienen un ID numerico largo
            if "/propiedades/clasificado/" in href or ("propiedades" in href and "clasificado" in href):
                full = href if href.startswith("http") else self.cfg["base_url"] + href
                detail_links.append(full)
        return list(set(detail_links))

    def extract_detail(self, page, url: str) -> dict:
        html = str(page.html_content)
        data = {"url": url, "titulo": page.css("title::text").get("")}
        
        # Intentar JSON-LD primero (mas preciso que regex)
        precio_ld = extraer_precio_jsonld(html)
        if precio_ld:
            data["precio"] = precio_ld
        else:
            # Fallback: regex en texto plano
            prices = re.findall(r'[$]\s*([1-9][0-9]{3,}(?:[,\\.][0-9]{2,3})?)', html)
            valid_prices = []
            for p in prices:
                p_clean = p.replace(",", "")
                try:
                    val = float(p_clean)
                    if val > 10000:
                        valid_prices.append(int(val))
                except ValueError:
                    pass
            if valid_prices:
                data["precio"] = valid_prices[0]
        
        # JSON-LD para ubicacion
        ubic = extraer_ubicacion_jsonld(html)
        data.update({k: v for k, v in ubic.items() if v})
        
        # Recámaras
        rec = re.findall(r'([0-9]+)\s*(?:recámara|recamara|habitacion)', html, re.I)
        if rec:
            data["recamaras"] = int(rec[0])
        # Baños
        ban = re.findall(r'([0-9]+)\s*(?:baño|bano)', html, re.I)
        if ban:
            data["banos"] = int(ban[0])
        # Metros
        m2 = re.findall(r'([0-9,]+)\s*(?:m²|m2|metros\s*c)(?:onstruidos)?', html, re.I)
        if m2:
            data["metros_cuadrados"] = int(m2[0].replace(",",""))
        # Colonia
        col = re.findall(r'Col(?:onia)?\.\s*([A-ZÁÉÍÓÚa-záéíóúñ]+)', html)
        if col:
            data["colonia"] = col[0].strip()
        # Imágenes
        imgs = re.findall(r'(https://[^"\']+\.(?:jpg|jpeg|webp))', html)
        data["fotos"] = list(set(imgs))[:5]
        data["tipo_inmueble"] = "casa" if "casa" in html.lower()[:5000] else "departamento"
        return data


class VivanunciosConnector:
    def __init__(self, config):
        self.cfg = config

    def extract_links(self, page) -> list:
        links = page.css('a::attr(href)')
        detail_links = []
        for l in links:
            href = str(l)
            if any(x in href for x in ["anuncio/", "inmueble/"]):
                full = href if href.startswith("http") else self.cfg["base_url"] + href
                detail_links.append(full)
        return list(set(detail_links))

    def extract_detail(self, page, url: str) -> dict:
        html = str(page.html_content)
        data = {"url": url, "titulo": page.css("title::text").get("")}
        # Precios grandes
        prices = re.findall(r'[$]\s*([1-9][0-9,]{4,})', html)
        if prices:
            data["precio"] = int(prices[0].replace(",", ""))
        rec = re.findall(r'([0-9]+)\s*(?:recámara|recamara|habitacion)', html, re.I)
        if rec:
            data["recamaras"] = int(rec[0])
        ban = re.findall(r'([0-9]+)\s*(?:baño|bano)', html, re.I)
        if ban:
            data["banos"] = int(ban[0])
        m2 = re.findall(r'([0-9,]+)\s*(?:m²|m2|metros)', html, re.I)
        if m2:
            data["metros_cuadrados"] = int(m2[0].replace(",",""))
        data["tipo_inmueble"] = "casa" if "casa" in html.lower()[:5000] else "departamento"
        return data


class LamudiConnector:
    def __init__(self, config):
        self.cfg = config

    def extract_links(self, page) -> list:
        links = page.css('a[href*="/detalle/"]::attr(href)')
        detail_links = []
        for l in links:
            href = str(l)
            full = f"{self.cfg['base_url']}{href}" if href.startswith("/") else href
            detail_links.append(full)
        return list(set(detail_links))

    def extract_detail(self, page, url: str) -> dict:
        html = str(page.html_content)
        data = {"url": url, "titulo": page.css("title::text").get("")}
        # Precios grandes
        prices = re.findall(r'[$]\s*([1-9][0-9,]{4,})', html)
        if prices:
            data["precio"] = int(prices[0].replace(",", ""))
        rec = re.findall(r'([0-9]+)\s*(?:habitacion|bedroom|recamara)', html, re.I)
        if rec:
            data["recamaras"] = int(rec[0])
        ban = re.findall(r'([0-9]+)\s*(?:baño|bano|bath)', html, re.I)
        if ban:
            data["banos"] = int(ban[0])
        m2 = re.findall(r'([0-9,]+)\s*(?:m²|m2|metros)', html, re.I)
        if m2:
            data["metros_cuadrados"] = int(m2[0].replace(",",""))
        col = re.findall(r'Col(?:onia)?\.\s*([A-ZÁÉÍÓÚa-záéíóúñ]+)', html)
        if col:
            data["colonia"] = col[0].strip()
        imgs = re.findall(r'(https://[^"\']+\.(?:jpg|jpeg|webp))', html)
        data["fotos"] = list(set(imgs))[:5]
        return data


# ── ORQUESTADOR ─────────────────────────────────────────────

def run_portal(name: str, cfg_portal: dict, settings: dict, seen: set):
    log.info(f"═══ {name.upper()} ═══")
    
    if not HAS_SCRAPLING:
        log.error("Scrapling no instalado. Usa: pip install scrapling curl-cffi browserforge patchright")
        return
    
    fetcher_type = cfg_portal["fetcher"]
    max_props = settings.get("max_properties_per_portal", 5)

    # Elegir conector
    if name == "inmuebles24":
        connector = Inmuebles24Connector(cfg_portal)
    elif name == "vivanuncios":
        connector = VivanunciosConnector(cfg_portal)
    elif name == "lamudi":
        connector = LamudiConnector(cfg_portal)
    else:
        log.warning(f"Sin conector para {name}")
        return

    # Fetch listing
    url = cfg_portal["listing_url"]
    log.info(f"Fetching listing: {url}")

    if fetcher_type == "stealthy":
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            solve_cloudflare=cfg_portal.get("solve_cloudflare", False),
            timeout=45000,
        )
    elif fetcher_type == "dynamic":
        page = DynamicFetcher.fetch(url, headless=True, network_idle=True)
    else:
        log.error(f"Fetcher desconocido: {fetcher_type}")
        return

    if page.status != 200:
        log.warning(f"  Status {page.status}, skipping")
        return

    # Extraer links
    links = connector.extract_links(page)
    log.info(f"  → {len(links)} links a detalle encontrados")

    if not links:
        log.info("  Sin links, probando con patrones alternativos...")
        html = str(page.html_content) if hasattr(page, 'html_content') else str(page)
        extra = re.findall(r"""(?:href|data-href)=["']([^"']*(?:detalle|inmueble|propiedad|anuncio)[^"']*)["']""", html)
        log.info(f"  Regex extra: {len(extra)}")
        links = list(set(extra))[:max_props]

    links = links[:max_props]
    saved = 0

    for i, detail_url in enumerate(links):
        if saved >= max_props:
            break
        log.info(f"  [{i+1}/{len(links)}] Detalle: {detail_url[:80]}...")
        try:
            if fetcher_type == "stealthy":
                detail_page = StealthyFetcher.fetch(
                    detail_url,
                    headless=True,
                    network_idle=True,
                    solve_cloudflare=cfg_portal.get("solve_cloudflare", False),
                    timeout=45000,
                )
            else:
                detail_page = DynamicFetcher.fetch(detail_url, headless=True, network_idle=True)

            if detail_page.status != 200:
                log.warning(f"    Status {detail_page.status}")
                continue

            data = connector.extract_detail(detail_page, detail_url)
            prop = normalize_property(data, name)

            # Si la calidad es baja (< 0.7), intentar con IA
            score = prop.get("calidad", {}).get("score", 0)
            if score < 0.7:
                log.info(f"    ↻ Score bajo ({score}), intentando con IA (Ling+Ring)...")
                html = str(detail_page.html_content) if hasattr(detail_page, 'html_content') else str(detail_page)
                ia_data = extraer_con_ia(html)
                if ia_data and ia_data.get("precio"):
                    # Merge: solo reemplazar campos que la IA extrajo correctamente
                    for campo in ["precio", "recamaras", "banos", "metros_cuadrados", "direccion", "colonia", "ciudad", "tipo_inmueble", "tipo_operacion"]:
                        if campo in ia_data and ia_data[campo] is not None:
                            data[campo] = ia_data[campo]
                    prop = normalize_property(data, name)
                    log.info(f"    ✓ IA corrigio: ${prop.get('precio',0):,} | score:{prop.get('calidad',{}).get('score',0)}")

            if save_property(prop, seen):
                saved += 1
                score = prop.get("calidad", {}).get("score", 0)
                flags = prop.get("calidad", {}).get("flags", [])
                flag_str = f" ⚠️{','.join(flags)}" if flags else ""
                log.info(f"    ✓ {prop['titulo'][:50]} | ${prop.get('precio',0):,} | score:{score}{flag_str}")
            time.sleep(settings.get("request_delay", 1.0))
        except Exception as e:
            log.warning(f"    ✗ Error: {e}")

    log.info(f"  Guardadas: {saved}")


# ── CLASE ORQUESTADORA PRINCIPAL ────────────────────────────

class PADIMScraper:
    """
    Orquestador principal de scraping.

    Uso:
        scraper = PADIMScraper()
        propiedades = scraper.scrape(portal="inmuebles24")  # opcional: filtrar por portal
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or str(CONFIG_PATH)
        self.config = self._load_config()
        self.settings = self.config.get("settings", {})
        self.seen: set = set()
        self._load_existing_fingerprints()

    def _load_config(self) -> dict:
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def _load_existing_fingerprints(self):
        """Carga fingerprints existentes para evitar duplicados entre ejecuciones."""
        output_path = DATA_DIR / "propiedades.jsonl"
        if output_path.exists():
            with open(output_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            existing = json.loads(line)
                            if existing.get("fingerprint"):
                                self.seen.add(existing["fingerprint"])
                        except json.JSONDecodeError:
                            pass
            log.info(f"Fingerprints cargados: {len(self.seen)}")

    def scrape(self, portal: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Ejecuta scraping de portales configurados.

        Args:
            portal: Opcional, nombre del portal a scrapear (ej: "inmuebles24", "vivanuncios", "lamudi").
                    Si es None, ejecuta todos los portales habilitados.

        Returns:
            Lista de diccionarios con propiedades scrapeadas.
        """
        DATA_DIR.mkdir(exist_ok=True)
        resultados = []

        if portal:
            cfg = self.config.get("portals", {}).get(portal)
            if not cfg:
                log.error(f"Portal '{portal}' no encontrado en config.yaml")
                return []
            if not cfg.get("enabled", True):
                log.warning(f"Portal '{portal}' está deshabilitado en config")
                return []
            try:
                run_portal(portal, cfg, self.settings, self.seen)
            except Exception as e:
                log.error(f"Error en portal {portal}: {e}")
        else:
            for name, cfg in self.config.get("portals", {}).items():
                if cfg.get("enabled", True):
                    try:
                        run_portal(name, cfg, self.settings, self.seen)
                    except Exception as e:
                        log.error(f"Error en portal {name}: {e}")
                    time.sleep(2)

        # Cargar resultados del archivo
        output_path = DATA_DIR / "propiedades.jsonl"
        if output_path.exists():
            with open(output_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            resultados.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

        total = len(resultados)
        log.info(f"\n═══ COMPLETADO ═══")
        log.info(f"Propiedades totales: {total}")
        log.info(f"Output: {output_path.resolve()}")
        return resultados


# ── MAIN (para ejecución directa) ───────────────────────────

def main():
    scraper = PADIMScraper()
    scraper.scrape()


if __name__ == "__main__":
    main()
