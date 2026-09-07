#!/usr/bin/env python3
"""Run PDF attachment rendering in this installation's private runtime."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
python = root / 'venv/bin/python'
if not python.is_file():
    raise SystemExit('Use the skill from an installed Meal Concierge runtime or its generated client package.')
os.execv(str(python), [str(python), '-I', str(root / 'pdf_pages.py'), *sys.argv[1:]])
