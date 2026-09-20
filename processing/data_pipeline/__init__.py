"""Orchestration of the repository's existing four data-processing stages."""
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
for directory in ("training-data-builder", "coordinate-converter"):
    sys.path.insert(0, str(PROJECT / "processing" / directory))
