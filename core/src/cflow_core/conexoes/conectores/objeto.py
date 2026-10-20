"""Connector for the `objeto` connection type: the DESTINATION of the medallion lake.

It is not a data source: it is where ingestion writes. It implements only `testar` (the admin needs
to check the credential and bucket access BEFORE scheduling the first ingestion); schema browsing
and sampling do not apply.

config: {provedor: 'local'|'gcs', raiz? (local), buckets? {landing,bronze,quarantine} (gcs),
         prefixo?, chave_kms?, timeout_segundos?}.
Secret (gcs) = service-account JSON, kept in the vault.
"""

from __future__ import annotations

from ...errors import ErroAplicacao
from .base import ResultadoTeste


def _nao_aplicavel() -> ErroAplicacao:
    return ErroAplicacao(
        codigo="nao_aplicavel",
        mensagem="Object-storage connection is a lake destination, not a data source.",
        status=400,
    )


class ConectorObjeto:
    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        from ...ingestao import armazenamento

        try:
            resultado = await armazenamento.montar("objeto", config, senha).testar()
        except ErroAplicacao as exc:
            return ResultadoTeste(ok=False, causa=exc.mensagem)
        except Exception:  # noqa: BLE001 — sanitised cause; never echoes the credential
            return ResultadoTeste(ok=False, causa="failed to access the lake storage")
        return ResultadoTeste(ok=bool(resultado.get("ok")), causa=resultado.get("causa"))

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        raise _nao_aplicavel()

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        raise _nao_aplicavel()

    async def amostra(
        self, config: dict, senha: str | None, sql: str, limite: int, params: dict | None = None
    ) -> tuple[list[str], list[dict]]:
        raise _nao_aplicavel()
