"""Geometry validation + symmetry analysis — pymatgen-backed (BACKENDS.md §7–8)."""

from __future__ import annotations

import numpy as np
from pymatgen.core import Structure
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

MIN_SITE_DIST = 0.5     # Å — below this, sites are considered overlapping
ANGLE_RANGE = (20.0, 160.0)
SLAB_MIN_VACUUM = 10.0  # Å


def validate_geometry(structure: Structure, calc_type: str = "generic") -> dict:
    """Deterministic sanity checks. calc_type: generic | slab | surface."""
    issues: list[str] = []
    n_checks = 0

    # 1. overlapping sites (pymatgen validity guard)
    n_checks += 1
    if len(structure) > 1:
        dm = structure.distance_matrix
        np.fill_diagonal(dm, np.inf)
        dmin = float(dm.min())
        if dmin < MIN_SITE_DIST:
            i, j = np.unravel_index(int(dm.argmin()), dm.shape)
            issues.append(
                f"overlapping sites: {structure[int(i)].specie}{int(i)}–"
                f"{structure[int(j)].specie}{int(j)} at {dmin:.3f} Å (< {MIN_SITE_DIST} Å)"
            )

    # 2. degenerate lattice angles
    n_checks += 1
    for name, ang in zip(("alpha", "beta", "gamma"), structure.lattice.angles):
        if not ANGLE_RANGE[0] <= ang <= ANGLE_RANGE[1]:
            issues.append(f"degenerate lattice angle {name}={ang:.1f}° "
                          f"(outside {ANGLE_RANGE[0]}–{ANGLE_RANGE[1]}°)")

    # 3. positive volume
    n_checks += 1
    if structure.lattice.volume < 1.0:
        issues.append(f"near-zero cell volume: {structure.lattice.volume:.3f} Å³")

    # 4. all atoms inside the cell (project convention; live-use finding on
    # make_interface, DEVLOG step 56)
    n_checks += 1
    tol = 1e-6
    outside = [
        i for i, f in enumerate(structure.frac_coords)
        if any(x < -tol or x >= 1.0 + tol for x in f)
    ]
    if outside:
        preview = ", ".join(str(i) for i in outside[:5])
        issues.append(
            f"{len(outside)} atom(s) outside the unit cell (fractional coords "
            f"beyond [0,1)): sites {preview}{'…' if len(outside) > 5 else ''} — "
            "wrap them into the cell"
        )

    # 5. slab-specific: vacuum thickness
    if calc_type in ("slab", "surface"):
        n_checks += 1
        z = [s.coords[2] for s in structure]
        vacuum = float(structure.lattice.c) - (max(z) - min(z))
        if vacuum < SLAB_MIN_VACUUM:
            issues.append(f"thin vacuum: {vacuum:.2f} Å (< {SLAB_MIN_VACUUM} Å) — "
                          f"periodic slab images will interact")

    return {
        "valid": not issues,
        "issues": issues,
        "n_checks": n_checks,
        "calc_type": calc_type,
    }


def analyze_symmetry(structure: Structure, symprec: float = 0.1) -> dict:
    """Spacegroup + inequivalent-site summary via spglib."""
    sga = SpacegroupAnalyzer(structure, symprec=symprec)
    sym = sga.get_symmetrized_structure()
    return {
        "spacegroup_symbol": sga.get_space_group_symbol(),
        "spacegroup_number": sga.get_space_group_number(),
        "crystal_system": sga.get_crystal_system(),
        "point_group": sga.get_point_group_symbol(),
        "n_inequivalent_sites": len(sym.equivalent_indices),
        "symprec": symprec,
    }
