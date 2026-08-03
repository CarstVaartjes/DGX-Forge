import sys
from pathlib import Path


CONTROL_SRC = Path(__file__).resolve().parents[3] / "control/src"
if str(CONTROL_SRC) not in sys.path:
    sys.path.insert(0, str(CONTROL_SRC))
