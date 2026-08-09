"""Resolve a notebook to the .py source it was generated from.

Declared pairings are resolved by jupytext's in-file frontmatter or project config files.
Undeclared pairing candidates are resolved by finding same-name .py files and checking them
for a jupytext header, `# %%` markers, or cells that match the notebook. This misses pairs when
no jupytext metadata is stored in either file (`notebook_metadata_filter = "-all"`) and the
py:light format is used.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import jupytext
from jupytext.config import (
    find_jupytext_configuration_file,
    get_formats_from_notebook_and_config,
    load_jupytext_configuration_file,
)
from jupytext.header import header_to_metadata_and_cell
from jupytext.paired_paths import paired_paths

__all__ = ["read_jupytext_config", "looks_like_notebook_source", "paired_source", "repo_relative", "cell_inputs"]

_log = logging.getLogger(__name__)

# Percent-format cell marker. Hand-written `# %%` scripts carry these too, and those count.
PERCENT_RE = re.compile(r"^#\s*%%", re.M)


def read_jupytext_config(root: Path):
    """Repo-level jupytext settings, or None if absent or unreadable."""
    try:
        path = find_jupytext_configuration_file(root, search_parent_dirs=False)
    except Exception:  # unreadable pyproject.toml, permissions, ...
        return None
    if path is None:
        return None
    try:
        return load_jupytext_configuration_file(path)
    except Exception as exc:
        # Publishing does not depend on the config, so a bad one warns and yields to defaults.
        # Parser errors span several lines; the first carries the reason.
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        _log.warning("%s could not be read (%s); using defaults", Path(path).name, reason)
        return None


def looks_like_notebook_source(py_path: Path) -> bool:
    """Does this .py read as a jupytext text representation? Header or `# %%` markers.

    False means unconfirmed, not disproven: `notebook_metadata_filter = "-all"` strips the header,
    and the light, nomarker, and sphinx formats emit no markers at all.
    """
    try:
        with py_path.open("r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    try:
        metadata, *_ = header_to_metadata_and_cell(head.splitlines(), "#", "", ".py")
    except Exception:
        metadata = {}  # truncated or malformed header
    return "jupytext" in metadata or bool(PERCENT_RE.search(head))


def paired_source(nb, nb_path: Path, root: Path, config=None) -> str | None:
    """Repo-relative .py peer of a notebook, or None if there is none."""
    peers = []
    try:
        fmts = get_formats_from_notebook_and_config(nb, config, str(nb_path))
        peers = paired_paths(str(nb_path), "ipynb", fmts)
    except Exception:  # unresolvable pairing: fall through to the guess
        pass

    for path, _fmt in peers:
        if Path(path).suffix == ".py":
            return repo_relative(Path(path), root)

    guess = nb_path.with_suffix(".py")
    rel = repo_relative(guess, root)
    if rel is None:
        return None
    if looks_like_notebook_source(guess):
        return rel

    try:  # a missing or unparseable file lands here too
        py_nb = jupytext.read(guess)
    except Exception:
        return None
    return rel if cell_inputs(py_nb) == cell_inputs(nb) else None


def repo_relative(path: Path, root: Path) -> str | None:
    """Posix path relative to the repo root, or None if it escapes."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def cell_inputs(nb) -> list[tuple[str, str]]:
    """(type, source) per cell, markdown and raw included."""
    return [(c.cell_type, c.source.strip()) for c in nb.cells]
