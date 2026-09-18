"""Local Materials Project database search — backend decision: derived SQLite artifact
over the downloader's JSONL snapshot (BACKENDS.md §11).

The gzip JSONL snapshot produced by `materials_project/download_mp_summary_structures.py`
(one summary document per line, `structure` stored as `Structure.as_dict()`) remains the
**source of truth and the contract** with that downloader; filter semantics mirror
`materials_project/local_mp_database.py`, reimplemented here so the plugin stays
self-contained.

Queries do NOT scan the snapshot: a full scan json-parses ~170k complete `structure`
dicts (~22 s on the production host), and five concurrent searches starved the MCP event
loop long enough for opencode to declare the server dead and drop it for the rest of the
container's life (step 182 incident). Instead, both `search()` and `get_structure()` read
a SQLite artifact derived from the snapshot (`<snapshot>.sqlite`: indexed summary columns
+ per-row zlib-compressed structure blobs). It is built automatically on first use
(stale-checked against the snapshot's size+mtime, atomic tmp+rename), next to the
snapshot — or, when that directory is read-only (production mounts the MP snapshot ro),
under `SEED_MP_INDEX_DIR` (default `~/.cache/seed-mcp`). Read-only connections are opened
per call, so concurrent tool calls never contend. Any artifact failure falls back to the
full-snapshot scan; results are identical either way (snapshot file order preserved).
Prebuild for a deployment with `python -m mason_mcp.runtime.matdb [snapshot.jsonl.gz]`.

Database location, in precedence order: explicit argument, `SEED_MP_DB` env var, then
`materials_project/data/mp_summary_structure.jsonl.gz` relative to the working directory
(opencode launches from the project root).
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import threading
import warnings
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from pymatgen.core import Structure

ENV_VAR = "SEED_MP_DB"
DEFAULT_DB = Path("materials_project") / "data" / "mp_summary_structure.jsonl.gz"
INDEX_ENV_VAR = "SEED_MP_INDEX_DIR"
SCHEMA_VERSION = 1

# Compact per-material summary returned by search (never the full structure — too big).
SUMMARY_FIELDS = (
    "material_id", "formula_pretty", "nelements", "nsites", "band_gap", "is_metal",
    "energy_above_hull", "formation_energy_per_atom", "theoretical",
)


def resolve_db(db_path: str | Path | None = None) -> Path:
    p = Path(db_path or os.environ.get(ENV_VAR) or DEFAULT_DB)
    if not p.is_file():
        raise FileNotFoundError(
            f"local MP database not found: {p} — launch from the project root, "
            f"set ${ENV_VAR}, or pass db_path (build the snapshot with "
            "materials_project/download_mp_summary_structures.py)"
        )
    return p


def iter_documents(db: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(db, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _as_range(value: Sequence[float] | float | int | None) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value), float(value)
    if len(value) != 2:
        raise ValueError(f"range filter must be [min, max], got {value!r}")
    return float(value[0]), float(value[1])


def _in_range(value: Any, bounds: tuple[float, float] | None) -> bool:
    if bounds is None:
        return True
    if value is None:
        return False
    return bounds[0] <= float(value) <= bounds[1]


def _normalize_chemsys(elements: Sequence[str] | str) -> str:
    parts = elements.split("-") if isinstance(elements, str) else list(elements)
    return "-".join(sorted(p for p in map(str, parts) if p))


def _summary_row(doc: dict[str, Any]) -> dict[str, Any]:
    row = {field: doc.get(field) for field in SUMMARY_FIELDS}
    symmetry = doc.get("symmetry") or {}
    row["spacegroup_symbol"] = symmetry.get("symbol")
    row["spacegroup_number"] = symmetry.get("number")
    return row


# ------------------------------------------------------------- SQLite artifact
# Summary columns in _summary_row's key order; `elements`/`chemsys`/`deprecated`
# serve the remaining filters, `structure` (zlib blob) serves get_structure.
_SUMMARY_COLS = SUMMARY_FIELDS + ("spacegroup_symbol", "spacegroup_number")

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE materials (
    ord INTEGER PRIMARY KEY,      -- snapshot file order -> deterministic results
    material_id TEXT UNIQUE,
    formula_pretty TEXT,
    nelements INTEGER,
    nsites INTEGER,
    band_gap REAL,
    is_metal INTEGER,
    energy_above_hull REAL,
    formation_energy_per_atom REAL,
    theoretical INTEGER,
    spacegroup_symbol TEXT,
    spacegroup_number INTEGER,
    deprecated INTEGER NOT NULL,
    chemsys TEXT,
    elements TEXT,                -- JSON array, sorted
    structure BLOB                -- zlib(json), NULL if the document had none
);
CREATE INDEX idx_formula ON materials(formula_pretty);
CREATE INDEX idx_chemsys ON materials(chemsys);
"""

_build_lock = threading.Lock()  # concurrent first calls must build once, not five times


def _artifact_candidates(db: Path) -> list[Path]:
    name = db.name + ".sqlite"
    fallback = Path(os.environ.get(INDEX_ENV_VAR)
                    or Path.home() / ".cache" / "seed-mcp")
    return [db.parent / name, fallback / name]


def _source_meta(db: Path) -> dict[str, str]:
    st = db.stat()
    return {"schema_version": str(SCHEMA_VERSION),
            "source_size": str(st.st_size), "source_mtime_ns": str(st.st_mtime_ns)}


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _artifact_is_fresh(artifact: Path, meta: dict[str, str]) -> bool:
    if not artifact.is_file():
        return False
    try:
        con = _open_ro(artifact)
        try:
            stored = dict(con.execute("SELECT key, value FROM meta"))
        finally:
            con.close()
        return {k: stored.get(k) for k in meta} == meta
    except sqlite3.Error:
        return False


def _bool_or_none(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _doc_records(db: Path) -> Iterator[tuple]:
    for ord_, doc in enumerate(iter_documents(db)):
        symmetry = doc.get("symmetry") or {}
        elements = sorted(str(e) for e in doc.get("elements") or [])
        structure = doc.get("structure")
        yield (
            ord_, str(doc.get("material_id")), doc.get("formula_pretty"),
            doc.get("nelements"), doc.get("nsites"), doc.get("band_gap"),
            _bool_or_none(doc.get("is_metal")), doc.get("energy_above_hull"),
            doc.get("formation_energy_per_atom"), _bool_or_none(doc.get("theoretical")),
            symmetry.get("symbol"), symmetry.get("number"),
            int(bool(doc.get("deprecated"))), "-".join(elements), json.dumps(elements),
            zlib.compress(json.dumps(structure).encode()) if structure else None,
        )


def _build_artifact(db: Path, artifact: Path, meta: dict[str, str]) -> None:
    """Full snapshot scan → SQLite artifact. Atomic tmp+rename so a concurrent
    reader/builder in another process never sees a partial database."""
    artifact.parent.mkdir(parents=True, exist_ok=True)
    tmp = artifact.with_name(artifact.name + f".tmp{os.getpid()}")
    try:
        con = sqlite3.connect(tmp)
        try:
            con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
            con.executescript(_SCHEMA)
            con.executemany(
                "INSERT INTO materials VALUES (%s)" % ",".join("?" * 16),
                _doc_records(db))
            con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            con.commit()
        finally:
            con.close()
        tmp.replace(artifact)
    finally:
        tmp.unlink(missing_ok=True)


def _artifact_path(db: Path) -> Path | None:
    """Fresh artifact for this snapshot, building it if needed. None means
    'no usable artifact' — callers fall back to the full-snapshot scan, so
    the artifact can only ever make things faster, never break a query."""
    meta = _source_meta(db)
    candidates = _artifact_candidates(db)
    with _build_lock:
        for candidate in candidates:
            if _artifact_is_fresh(candidate, meta):
                return candidate
        for candidate in candidates:  # stale/missing everywhere -> (re)build
            try:
                _build_artifact(db, candidate, meta)
                return candidate
            except (OSError, sqlite3.Error):
                continue  # e.g. read-only snapshot dir -> try the fallback dir
    return None


def _record_to_summary(record: Sequence[Any]) -> dict[str, Any]:
    row = dict(zip(_SUMMARY_COLS, record))
    for key in ("is_metal", "theoretical"):  # SQLite stores 0/1, callers expect bools
        if row[key] is not None:
            row[key] = bool(row[key])
    return row


def search(
    *,
    elements: Sequence[str] | None = None,
    exclude_elements: Sequence[str] | None = None,
    chemsys: str | None = None,
    formula: str | None = None,
    material_ids: Sequence[str] | None = None,
    spacegroup_symbol: str | None = None,
    spacegroup_number: int | None = None,
    band_gap: Sequence[float] | None = None,
    energy_above_hull: Sequence[float] | None = None,
    nsites: Sequence[int] | int | None = None,
    nelements: Sequence[int] | int | None = None,
    is_metal: bool | None = None,
    theoretical: bool | None = None,
    include_deprecated: bool = False,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return up to `limit` summary rows in snapshot file order (deterministic)."""
    if limit < 1:
        raise ValueError("limit must be a positive integer")

    db = resolve_db(db_path)
    required = {str(e) for e in elements} if elements else None
    rejected = {str(e) for e in exclude_elements} if exclude_elements else None
    chemsys_norm = _normalize_chemsys(chemsys) if chemsys else None
    id_set = {str(i) for i in material_ids} if material_ids else None
    band_gap_r = _as_range(band_gap)
    e_hull_r = _as_range(energy_above_hull)
    nsites_r = _as_range(nsites)
    nelements_r = _as_range(nelements)

    artifact = _artifact_path(db)
    if artifact is not None:
        try:
            return _search_sqlite(
                artifact, required=required, rejected=rejected,
                chemsys_norm=chemsys_norm, id_set=id_set, formula=formula,
                spacegroup_symbol=spacegroup_symbol,
                spacegroup_number=spacegroup_number, band_gap_r=band_gap_r,
                e_hull_r=e_hull_r, nsites_r=nsites_r, nelements_r=nelements_r,
                is_metal=is_metal, theoretical=theoretical,
                include_deprecated=include_deprecated, limit=limit)
        except sqlite3.Error:
            pass  # fall through to the snapshot scan

    rows: list[dict[str, Any]] = []
    truncated = False
    for doc in iter_documents(db):
        if not include_deprecated and doc.get("deprecated"):
            continue
        if id_set and str(doc.get("material_id")) not in id_set:
            continue
        if formula and str(doc.get("formula_pretty")) != formula:
            continue

        doc_elements = {str(e) for e in doc.get("elements") or []}
        if required and not required.issubset(doc_elements):
            continue
        if rejected and rejected & doc_elements:
            continue
        if chemsys_norm and _normalize_chemsys(doc_elements) != chemsys_norm:
            continue

        if not (_in_range(doc.get("band_gap"), band_gap_r)
                and _in_range(doc.get("energy_above_hull"), e_hull_r)
                and _in_range(doc.get("nsites"), nsites_r)
                and _in_range(doc.get("nelements"), nelements_r)):
            continue

        if is_metal is not None and doc.get("is_metal") != is_metal:
            continue
        if theoretical is not None and doc.get("theoretical") != theoretical:
            continue

        symmetry = doc.get("symmetry") or {}
        if spacegroup_symbol and str(symmetry.get("symbol")) != spacegroup_symbol:
            continue
        if spacegroup_number is not None and symmetry.get("number") != spacegroup_number:
            continue

        rows.append(_summary_row(doc))
        if len(rows) >= limit:
            # file order is stable, so "the first `limit` matches" is deterministic
            truncated = True
            break

    return {"n_results": len(rows), "truncated": truncated, "results": rows}


def _search_sqlite(artifact: Path, *, required, rejected, chemsys_norm, id_set,
                   formula, spacegroup_symbol, spacegroup_number, band_gap_r,
                   e_hull_r, nsites_r, nelements_r, is_metal, theoretical,
                   include_deprecated, limit) -> dict[str, Any]:
    """The scan loop above translated to SQL: scalar filters pushed into WHERE
    (NULL never matches an equality/range filter, mirroring the scan's
    None-handling); element-set filters applied in Python on the streamed rows;
    ORDER BY ord reproduces snapshot file order exactly."""
    where, params = [], []
    if not include_deprecated:
        where.append("deprecated = 0")
    if id_set:
        where.append("material_id IN (%s)" % ",".join("?" * len(id_set)))
        params += sorted(id_set)
    if formula:
        where.append("formula_pretty = ?")
        params.append(formula)
    if chemsys_norm:
        where.append("chemsys = ?")
        params.append(chemsys_norm)
    for column, bounds in (("band_gap", band_gap_r), ("energy_above_hull", e_hull_r),
                           ("nsites", nsites_r), ("nelements", nelements_r)):
        if bounds is not None:
            where.append(f"{column} BETWEEN ? AND ?")
            params += [bounds[0], bounds[1]]
    if is_metal is not None:
        where.append("is_metal = ?")
        params.append(int(is_metal))
    if theoretical is not None:
        where.append("theoretical = ?")
        params.append(int(theoretical))
    if spacegroup_symbol:
        where.append("spacegroup_symbol = ?")
        params.append(spacegroup_symbol)
    if spacegroup_number is not None:
        where.append("spacegroup_number = ?")
        params.append(spacegroup_number)

    sql = "SELECT %s, elements FROM materials" % ", ".join(_SUMMARY_COLS)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ord"

    rows: list[dict[str, Any]] = []
    truncated = False
    con = _open_ro(artifact)
    try:
        for record in con.execute(sql, params):
            doc_elements = set(json.loads(record[-1] or "[]"))
            if required and not required.issubset(doc_elements):
                continue
            if rejected and rejected & doc_elements:
                continue
            rows.append(_record_to_summary(record[:-1]))
            if len(rows) >= limit:
                truncated = True
                break
    finally:
        con.close()
    return {"n_results": len(rows), "truncated": truncated, "results": rows}


def get_document(material_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    db = resolve_db(db_path)
    artifact = _artifact_path(db)
    if artifact is not None:
        try:
            return _get_document_sqlite(artifact, material_id)
        except sqlite3.Error:
            pass  # fall through to the snapshot scan
    for doc in iter_documents(db):
        if str(doc.get("material_id")) == material_id:
            return doc
    raise KeyError(f"material_id not found in local database: {material_id}")


def _get_document_sqlite(artifact: Path, material_id: str) -> dict[str, Any]:
    con = _open_ro(artifact)
    try:
        record = con.execute(
            "SELECT %s, deprecated, elements, structure FROM materials "
            "WHERE material_id = ?" % ", ".join(_SUMMARY_COLS),
            (material_id,)).fetchone()
    finally:
        con.close()
    if record is None:
        raise KeyError(f"material_id not found in local database: {material_id}")
    doc = _record_to_summary(record[:len(_SUMMARY_COLS)])
    doc["symmetry"] = {"symbol": doc.pop("spacegroup_symbol"),
                       "number": doc.pop("spacegroup_number")}
    doc["deprecated"] = bool(record[-3])
    doc["elements"] = json.loads(record[-2] or "[]")
    blob = record[-1]
    doc["structure"] = json.loads(zlib.decompress(blob)) if blob else None
    return doc


def get_structure(material_id: str, db_path: str | Path | None = None) -> tuple[Structure, dict[str, Any]]:
    doc = get_document(material_id, db_path)
    if not doc.get("structure"):
        raise ValueError(f"document {material_id} has no structure field")
    with warnings.catch_warnings():
        # MP site properties (magmom, forces, …) are per-material optional; pymatgen
        # warns for each missing one — known-benign for snapshot documents
        warnings.filterwarnings("ignore", message="Not all sites have property")
        structure = Structure.from_dict(doc["structure"])
    # snapshot site properties are partial (missing → None) and crash the POSCAR
    # writer (e.g. selective_dynamics=None); the wire format carries geometry only
    for prop in list(structure.site_properties):
        structure.remove_site_property(prop)
    # snapshot species may carry oxidation_state=None, which crashes pymatgen ops
    # downstream (e.g. Slab.is_polar multiplies oxi_state by occupancy)
    structure.remove_oxidation_states()
    return structure, _summary_row(doc)


if __name__ == "__main__":  # prebuild (deploy host / container boot): build or adopt
    import sys

    _db = resolve_db(sys.argv[1] if len(sys.argv) > 1 else None)
    _target = _artifact_path(_db)  # freshness check + ro-dir fallback, atomic build
    if _target is None:
        sys.exit(f"could not build SQLite artifact for {_db}: no writable location")
    print(f"SQLite artifact ready: {_target}")
