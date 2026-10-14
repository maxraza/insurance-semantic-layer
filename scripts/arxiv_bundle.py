"""Bundle the paper for arXiv: sources, figures and the compiled .bbl (arXiv does not run BibTeX).

Build the paper first so paper/build/main.bbl exists, then:
    uv run python scripts/arxiv_bundle.py   ->   dist/arxiv/ and dist/arxiv.tar.gz
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

PAPER = Path("paper")
OUT = Path("dist/arxiv")


def main() -> None:
    bbl = next((p for p in (PAPER / "build" / "main.bbl", PAPER / "main.bbl") if p.exists()), None)
    if bbl is None:
        raise SystemExit("no main.bbl: build the paper first (tectonic -k --outdir build main.tex, or latexmk)")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name in ("main.tex", "results_text.tex"):
        shutil.copy2(PAPER / name, OUT / name)
    shutil.copy2(bbl, OUT / "main.bbl")
    for sub in ("generated", "figures"):
        shutil.copytree(PAPER / sub, OUT / sub)
    tar = OUT.parent / "arxiv.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for f in sorted(OUT.rglob("*")):
            if f.is_file():
                t.add(f, arcname=f.relative_to(OUT).as_posix())
    print(f"wrote {OUT} and {tar}")


if __name__ == "__main__":
    main()
