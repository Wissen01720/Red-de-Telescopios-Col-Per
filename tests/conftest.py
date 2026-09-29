import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # astropy solo se usa como referencia en las pruebas; nunca descargar tablas IERS.
    from astropy.utils import iers

    iers.conf.auto_download = False
    iers.conf.iers_degraded_accuracy = "warn"
    warnings.filterwarnings("ignore", module="astropy")
    warnings.filterwarnings("ignore", module="erfa")
except ImportError:
    pass
