"""Run every pure test file; exit non-zero on any failure."""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
rc = 0
for f in sorted(here.glob("test_*.py")):
    print(f"=== {f.name} ===")
    rc |= subprocess.call([sys.executable, str(f)])
sys.exit(rc)
