"""Start-Skript für PyInstaller (relative Importe brauchen den Paket-Kontext)."""

import sys

from esxi_backupper.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
