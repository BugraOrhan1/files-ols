"""Learned tuning pipeline: training pairs -> frozen package -> generated BIN.

The pipeline is split into two executable parts:

* ``library`` / ``learn`` / ``package`` build a frozen, versioned tuning
  package from confirmed Original -> Stage 1 pairs found in WinOLS projects.
* ``generate`` / ``export`` apply a released package to a new original file
  and write a verified output without needing WinOLS or a parser.
"""

SOFTWARE_VERSION = "1.0.0"
