"""PyInstaller entry point for the OLS Tuner Windows app."""

import sys

from ols_platform.tuning.app import main

if __name__ == "__main__":
    sys.exit(main())
