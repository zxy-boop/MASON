"""Materials Project online access (api.materialsproject.org) for search_materials / get_material.

Used when no local snapshot is configured (SEED_MP_DB) but an API key is available in the environment
variable MP_API_KEY. Only the standard library and pymatgen are needed; the key travels in the X-API-KEY header.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pymatgen.core import Structure

ENV_KEY = "MP_API_KEY"
USER_AGENT = "MASON/1.0 (+https://github.com/zxy-boop/MASON)"
BASE = "https://api.materialsproject.org"
SUMMARY_FIELDS = ("material_id", "formula_pretty", "nelements", "nsites", "band_gap", "is_metal",
                  "energy_above_hull", "formation_energy_per_atom", "theoretical", "symmetry")


class MPError(RuntimeError):
    pass


def _ssl_context():
    """Default TLS context; falls back to certifi's CA bundle when the system has no root certificates
    (portable Python on a minimal Linux)."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        if not ctx.get_ca_certs() and not os.environ.get("SSL_CERT_FILE"):
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def api_key() -> str | None:
    k = os.environ.get(ENV_KEY, "").strip()
    return k or None


def _get(path: str, params: dict[str, Any], key: str, timeout: float = 60) -> dict[str, Any]:
    url = BASE + path + ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else "")
    # api.materialsproject.org sits behind Cloudflare, which rejects Python's default User-Agent (error 1010)
    req = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403):
            raise MPError(f"Materials Project rejected the API key (HTTP {e.code}). Check MP_API_KEY: run `MASON --setup` "
                          f"and enter the key from https://next-materialsproject.org/api. {body}")
        raise MPError(f"Materials Project API error HTTP {e.code} for {path}: {body}")
    except urllib.error.URLError as e:
        raise MPError(f"Materials Project API not reachable ({e.reason}); check the network connection")


def _symmetry(doc: dict[str, Any]) -> dict[str, Any]:
    sym = doc.get("symmetry") or {}
    return {"spacegroup_symbol": sym.get("symbol"), "spacegroup_number": sym.get("number"),
            "crystal_system": sym.get("crystal_system")}


def _row(doc: dict[str, Any]) -> dict[str, Any]:
    row = {k: doc.get(k) for k in SUMMARY_FIELDS if k != "symmetry"}
    row.update(_symmetry(doc))
    return row


def search(*, elements=None, exclude_elements=None, chemsys=None, formula=None, spacegroup_symbol=None,
           spacegroup_number=None, band_gap=None, energy_above_hull=None, nsites=None, nelements=None,
           is_metal=None, theoretical=None, limit: int = 20, key: str | None = None) -> dict[str, Any]:
    key = key or api_key()
    if not key:
        raise MPError("no Materials Project API key (MP_API_KEY)")
    p: dict[str, Any] = {"_fields": ",".join(SUMMARY_FIELDS), "_limit": int(limit), "_skip": 0}
    if elements: p["elements"] = ",".join(elements)
    if exclude_elements: p["exclude_elements"] = ",".join(exclude_elements)
    if chemsys: p["chemsys"] = chemsys
    if formula: p["formula"] = formula
    if spacegroup_symbol: p["spacegroup_symbol"] = spacegroup_symbol
    if spacegroup_number is not None: p["spacegroup_number"] = int(spacegroup_number)
    for name, rng in (("band_gap", band_gap), ("energy_above_hull", energy_above_hull), ("nsites", nsites), ("nelements", nelements)):
        if rng is None:
            continue
        lo, hi = (rng, rng) if isinstance(rng, (int, float)) else (rng[0], rng[1])
        p[f"{name}_min"] = lo; p[f"{name}_max"] = hi
    if is_metal is not None: p["is_metal"] = "true" if is_metal else "false"
    if theoretical is not None: p["theoretical"] = "true" if theoretical else "false"
    data = _get("/materials/summary/", p, key)
    docs = data.get("data", [])
    rows = [_row(d) for d in docs]
    meta = data.get("meta") or {}
    total = meta.get("total_doc")
    return {"n_results": len(rows), "truncated": bool(total and total > len(rows)), "total_available": total,
            "source": "Materials Project API", "results": rows}


def get_structure(material_id: str, key: str | None = None) -> tuple[Structure, dict[str, Any]]:
    key = key or api_key()
    if not key:
        raise MPError("no Materials Project API key (MP_API_KEY)")
    # the per-id path (/materials/summary/<id>/) is a deprecated endpoint that the API now blocks; query by material_ids
    data = _get("/materials/summary/", {"material_ids": material_id, "_fields": ",".join(SUMMARY_FIELDS + ("structure",)), "_limit": 1}, key)
    docs = data.get("data", [])
    if not docs:
        raise MPError(f"{material_id} not found in the Materials Project")
    doc = docs[0]
    structure = Structure.from_dict(doc["structure"])
    summary = _row(doc); summary["source"] = "Materials Project API"
    return structure, summary


def verify(key: str) -> dict[str, Any]:
    """One small request to check that a key works; returns the Si summary rows."""
    return search(formula="Si", energy_above_hull=[0, 0.001], limit=3, key=key)
