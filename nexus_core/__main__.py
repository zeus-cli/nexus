"""Permite ``python3 -m nexus_core ...`` sem instalar o pacote."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
