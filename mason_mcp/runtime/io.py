"""Structure I/O — backend decision: pymatgen first, ASE fallback (BACKENDS.md §1–2)."""

from __future__ import annotations

from pathlib import Path

from pymatgen.core import Structure

# Formats pymatgen writes natively; everything else goes through ASE.
_PMG_WRITE = {"poscar", "vasp", "cif", "json", "xsf"}


def load(path: str | Path) -> Structure:
    """Read any structure file. pymatgen native parse first; ASE's ~100 formats as fallback."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"structure file not found: {path}")
    try:
        return Structure.from_file(path)
    except Exception as pmg_err:  # noqa: BLE001 — deliberate fallback boundary
        try:
            import ase.io
            from pymatgen.io.ase import AseAtomsAdaptor

            atoms = ase.io.read(str(path))
            return AseAtomsAdaptor.get_structure(atoms)
        except Exception as ase_err:  # noqa: BLE001
            raise ValueError(
                f"neither pymatgen nor ASE could parse {path}: "
                f"pymatgen: {pmg_err}; ase: {ase_err}"
            ) from ase_err


def save(structure: Structure, path: str | Path, fmt: str | None = None) -> Path:
    """Write a structure. POSCAR/CIF via pymatgen (native); other formats via ASE.

    POSCAR species are GROUPED by element (user rule, 2026-07-17: layered
    builds otherwise interleave the species line — "Pd O Pd O …" with 8
    POTCAR blocks and unwritable MAGMOM counts). get_sorted_structure sorts
    by electronegativity; Python sorts are stable, so within an element the
    original (z-layer) order — and every per-site property, including the
    frozen-bottom selective_dynamics flags — rides along. Artifacts are the
    exchange contract, so tools that report site indices load from the file
    and stay consistent with the grouped order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or _infer_fmt(path)).lower()
    if fmt in _PMG_WRITE:
        if fmt in ("vasp", "poscar"):
            structure = structure.get_sorted_structure()
        structure.to(filename=str(path), fmt="poscar" if fmt == "vasp" else fmt)
    else:
        import ase.io
        from pymatgen.io.ase import AseAtomsAdaptor

        ase.io.write(str(path), AseAtomsAdaptor.get_atoms(structure), format=fmt)
    return path


def _infer_fmt(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("vasp", ""):
        return "poscar"
    return suffix


def describe(structure: Structure) -> dict:
    """Uniform structure summary used by every tool result."""
    lat = structure.lattice
    comp = structure.composition
    z = [s.coords[2] for s in structure]
    z_span = round(max(z) - min(z), 4) if z else 0.0
    return {
        "formula": comp.reduced_formula,
        "formula_full": str(comp.formula),
        "n_sites": len(structure),
        "species": {str(el): int(n) for el, n in sorted(comp.items(), key=lambda kv: str(kv[0]))},
        "lattice": {
            "abc": [round(x, 4) for x in lat.abc],
            "angles": [round(x, 2) for x in lat.angles],
            "volume": round(lat.volume, 4),
        },
        "slab_z_span": z_span,
        "vacuum_along_c": round(lat.c - z_span, 4),
    }
