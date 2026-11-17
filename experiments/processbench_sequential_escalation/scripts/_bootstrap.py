"""Make `src` importable from scripts/ and never write bytecode into frozen folders."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
STAGE_ROOT = Path(__file__).resolve().parent.parent
if str(STAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(STAGE_ROOT))
