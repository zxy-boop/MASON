"""Atom-site spec resolution (step 262 — the qidi O37 incident).

Three numbering conventions used to coexist (viewer 0-based global serial,
tool 0-based global ints, users' element-scoped "O37" reading) and the LLM
was doing the arithmetic between them — it replaced the wrong atom. The
unified, deterministic contract is:

  human-visible numbering is ALWAYS 1-based, shown in both forms
      viewer label:  "O37 · #69"
  tool site specs accept exactly:
      "O37"  -> the 37th O site (element-scoped, 1-based)
      "#69"  -> global site 69 (1-based)
      69     -> same as "#69" (ints are 1-based global)
  internal 0-based indices never cross the tool boundary.

resolve_site() does the mapping and VALIDATES it (an element-scoped spec
must land on that element; helpful errors otherwise) — the model passes the
user's own words through and never counts atoms itself.
"""

from __future__ import annotations

import re

_SPEC = re.compile(r"^\s*(?:#\s*(\d+)|([A-Z][a-z]?)\s*[-#]?\s*(\d+)|(\d+))\s*$")


def site_label(structure, index0: int) -> str:
    """Human label for an internal 0-based site: 'O37 · #69'."""
    el = structure[index0].specie.symbol
    nth = sum(1 for s in structure[: index0 + 1] if s.specie.symbol == el)
    return f"{el}{nth} · #{index0 + 1}"


def resolve_site(structure, spec) -> int:
    """Resolve a site spec to the internal 0-based index (validating hard)."""
    if spec is None:
        raise ValueError("no site given (accepted forms: 'O37', '#69' or 69)")
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        n = int(spec)
        if not 1 <= n <= len(structure):
            raise ValueError(
                f"global index {n} is out of range (1..{len(structure)}); indices start at 1, "
                "matching the # labels in the viewer")
        return n - 1
    m = _SPEC.match(str(spec))
    if not m:
        raise ValueError(f"cannot parse site '{spec}': accepted forms are 'O37' (the 37th O), "
                         "'#69' or 69 (global index, 1-based)")
    if m.group(1):  # "#69"
        return resolve_site(structure, int(m.group(1)))
    if m.group(4):  # bare digits inside a string with spaces
        return resolve_site(structure, int(m.group(4)))
    el, nth = m.group(2), int(m.group(3))
    hits = [i for i, s in enumerate(structure) if s.specie.symbol == el]
    if not hits:
        present = sorted({s.specie.symbol for s in structure})
        raise ValueError(f"the structure contains no {el} atoms (only {', '.join(present)})")
    if not 1 <= nth <= len(hits):
        raise ValueError(f"there are only {len(hits)} {el} atoms, '{el}{nth}' is out of range"
                         f" (per-element indices start at 1)")
    return hits[nth - 1]


def resolve_sites(structure, specs) -> list[int]:
    """Resolve a mixed list of specs to sorted unique 0-based indices."""
    out = {resolve_site(structure, item) for item in (specs or [])}
    return sorted(out)
