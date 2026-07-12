"""
PADIM Scrapers — Módulo de conectores inmobiliarios MX.

Exporta:
- PADIMScraper: clase orquestadora con método scrape()
- Inmuebles24Connector, VivanunciosConnector, LamudiConnector
- Funciones utilitarias (fingerprint, validar_calidad, detectar_moneda, etc.)
"""

from padim.scrapers.scraper_padim import PADIMScraper

__all__ = ["PADIMScraper"]
