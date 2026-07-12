#!/usr/bin/env python3
"""Check that all dependencies for PADIM scraping are installed."""
import importlib

deps = [
    "_cffi_backend",
    "yaml",
    "curl_cffi",
    "requests",
    "beautifulsoup4",
    "lxml",
]

ok = True
for d in deps:
    try:
        importlib.import_module(d)
        print(f"  ✅ {d}")
    except ImportError as e:
        print(f"  ❌ {d}: {e}")
        ok = False

print(f"\n→ {'All OK' if ok else 'SOME MISSING'}")
