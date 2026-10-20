"""Runtime helpers: file-mounted secrets and TLS context for the metadata Postgres.

- **File-mounted secrets** (Docker/Kubernetes style): every file `<dir>/<mount>/value` becomes the
  environment variable named after the directory (`cflow-vault-key` -> `CFLOW_VAULT_KEY`), never
  overwriting a variable that already exists. The directory defaults to `/run/secrets/cflow` and
  can be changed with `CFLOW_SECRETS_DIR`. Without the directory this is a no-op.
- **TLS** for the Postgres connection (`CFLOW_DATABASE_TLS`, `CFLOW_DATABASE_CA_FILE`).
"""

from __future__ import annotations

import logging
import os
import re
import ssl
from collections.abc import MutableMapping
from pathlib import Path

DIRETORIO_SEGREDOS = "/run/secrets/cflow"
TAMANHO_MAX_SEGREDO = 65_536
_NOME_VARIAVEL = re.compile(r"^[A-Z][A-Z0-9_]*$")

logger = logging.getLogger("cflow_core.runtime")


def nome_variavel(diretorio: str) -> str | None:
    """`cflow-vault-key` -> `CFLOW_VAULT_KEY`; a name that is not a valid variable -> None."""
    nome = diretorio.upper().replace("-", "_")
    return nome if _NOME_VARIAVEL.fullmatch(nome) else None


def exportar_segredos_arquivo(
    diretorio: str | Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Export every `<dir>/<mount>/value` as an environment variable. Returns the exported NAMES
    (never values). An already-defined variable wins over the file; a missing directory is a no-op."""
    env = os.environ if environ is None else environ
    raiz = Path(diretorio or os.environ.get("CFLOW_SECRETS_DIR") or DIRETORIO_SEGREDOS)
    if not raiz.is_dir():
        return []
    exportadas: list[str] = []
    for arquivo in sorted(raiz.glob("*/value")):
        if not arquivo.is_file():
            continue
        nome = nome_variavel(arquivo.parent.name)
        if nome is None:
            logger.warning(
                "segredo_ignorado",
                extra={"extra_campos": {"motivo": "directory name is not a valid variable"}},
            )
            continue
        if nome in env:
            continue
        with arquivo.open("rb") as f:
            bruto = f.read(TAMANHO_MAX_SEGREDO + 1)
        if len(bruto) > TAMANHO_MAX_SEGREDO:
            logger.warning(
                "segredo_ignorado",
                extra={"extra_campos": {"variavel": nome, "motivo": "too large"}},
            )
            continue
        try:
            valor = bruto.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            logger.warning(
                "segredo_ignorado",
                extra={"extra_campos": {"variavel": nome, "motivo": "not UTF-8"}},
            )
            continue
        env[nome] = valor
        exportadas.append(nome)
    if exportadas:
        logger.info("segredos_exportados", extra={"extra_campos": {"variaveis": exportadas}})
    return exportadas


def contexto_ssl(ca_file: str | None) -> ssl.SSLContext:
    """TLS context for the metadata Postgres.

    With a CA: verifies the chain (`verify-ca`) without checking the host name. Without a CA:
    encrypts without verifying (`require`). `VERIFY_X509_STRICT` is dropped because Python 3.13+
    rejects a CA without `keyUsage`, which libpq/psql accept; chain, signature and validity are
    still verified.
    """
    if ca_file and Path(ca_file).is_file():
        ctx = ssl.create_default_context(cafile=ca_file)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    else:
        if ca_file:
            logger.warning("ca_do_banco_ausente", extra={"extra_campos": {"modo": "require"}})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # Explicit floor: the default depends on the distribution's openssl.cnf.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
