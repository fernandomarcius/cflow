"""Ingestion of external public data into the medallion lake.

`landing` (immutable raw) -> `bronze` (Parquet, encoding already resolved). Connectors are chosen
by ACCESS CLASS and each source has a declarative contract; the destination is a pluggable object
backend: `local` by default, `gcs` by configuration.
"""

from __future__ import annotations

from . import armazenamento, bronze, contrato, leitura, motor, repositorio, service

__all__ = [
    "armazenamento",
    "bronze",
    "contrato",
    "leitura",
    "motor",
    "repositorio",
    "service",
]
