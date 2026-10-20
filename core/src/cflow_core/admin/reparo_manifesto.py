"""Audited restoration of an immutable manifest preserved in a lake backup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, AtorTipo, registrar
from ..errors import ErroAplicacao

BACKUP_RAIZ = "_legacy_layout_backup"
RECURSO_TIPO = "lake_manifesto_reparo"
VERSAO_REPARO = 1
DATASET_ANTIGO_POR_VIGENTE = {
    "ref/report/sheet_orders": "report_sheet_orders",
    "ref/report/summary_forecast": "report_summary_forecast",
    "ref/report/summary_orders": "report_summary_orders",
}


async def materializar_backup_layout_antigo(*, lake, caminho: str, destino: Path) -> str:
    """Read only the physical backup sibling of ``gold`` through the trusted local backend.

    The layout was preserved at ``<root>/_legacy_layout_backup``; treating it as an object of the
    ``gold`` layer would look under ``<root>/gold/_legacy_layout_backup``. There is no approved
    equivalent mapping for a remote backend: in that case the operation fails closed.
    """
    prefixo = f"{BACKUP_RAIZ}/"
    if (
        not isinstance(caminho, str)
        or not caminho.startswith(prefixo)
        or caminho == prefixo
        or any(parte in {"", ".", ".."} for parte in caminho.split("/"))
        or "\\" in caminho
    ):
        raise ValueError("backup path differs from the exact allowed root")
    if getattr(lake, "provedor", None) != "local":
        raise ValueError("legacy layout repair requires a local backend")
    materializar = getattr(lake, "materializar_backup_confiavel", None)
    if not callable(materializar):
        raise ValueError("local backend does not implement trusted backup reads")
    return await materializar(BACKUP_RAIZ, caminho[len(prefixo) :], destino)


def _identidade_reparo(destino: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"cflow:{RECURSO_TIPO}:{destino}")


def _depois_reparo(*, camada: str, dataset: str, origem: str, destino: str, sha256: str) -> dict:
    return {
        "versao": VERSAO_REPARO,
        "estado": "restaurado",
        "dataset": dataset,
        "camada": camada,
        "origem": origem,
        "destino": destino,
        "sha256": sha256,
    }


async def _travar_destino(conn: AsyncConnection, destino: str) -> None:
    """Serialize write and reconciliation; rollback/dead connection releases the lock."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:chave))"),
        {"chave": f"cflow:{RECURSO_TIPO}:{destino}"},
    )


async def _auditoria_reparo(conn: AsyncConnection, recurso_id: UUID) -> list[dict]:
    return [
        dict(linha)
        for linha in (
            (
                await conn.execute(
                    text(
                        "SELECT ator_id, ator_tipo, acao, antes, depois, request_id "
                        "FROM auditoria WHERE recurso_tipo=:tipo AND recurso_id=:id ORDER BY id"
                    ),
                    {"tipo": RECURSO_TIPO, "id": recurso_id},
                )
            )
            .mappings()
            .all()
        )
    ]


async def restaurar_manifesto_imutavel(
    *,
    conn: AsyncConnection,
    lake,
    ator_id: UUID,
    camada: str,
    dataset: str,
    origem: str,
    destino: str,
    conteudo: bytes,
    sha256: str,
) -> bool:
    """Write only a manifest that is missing and equivalent to the already validated plan.

    Returns ``True`` when it wrote and ``False`` when a re-run found exactly the same bytes. A
    divergent destination is never overwritten. A transactional advisory lock serializes each
    destination. The object is written before the trail: if the SQL commit fails after the write,
    the re-run verifies the bytes and reconciles the single canonical audit record without rewriting.
    """
    if camada != "gold":
        raise ValueError("manifest repair is restricted to the gold layer")
    if getattr(lake, "provedor", None) != "local":
        raise ValueError("legacy layout repair requires a local backend")
    antigo = DATASET_ANTIGO_POR_VIGENTE.get(dataset)
    if antigo is None:
        raise ValueError("dataset is not on the repair allow-list")
    if not destino.endswith("/_manifesto.json"):
        raise ValueError("repair destination is not a manifest")
    if any(
        "\\" in caminho or any(parte in {"", ".", ".."} for parte in caminho.split("/"))
        for caminho in (origem, destino)
    ):
        raise ValueError("invalid path in manifest repair")
    prefixo_destino = f"{dataset}/"
    if not destino.startswith(prefixo_destino):
        raise ValueError("repair destination does not belong to the allowed dataset")
    sufixo = destino[len(dataset) :]
    origem_esperada = f"{BACKUP_RAIZ}/{antigo}{sufixo}"
    if origem != origem_esperada:
        raise ValueError("repair source differs from the exact allowed backup")
    if hashlib.sha256(conteudo).hexdigest() != sha256:
        raise ValueError("sha256 of the planned manifest differs from the bytes")
    try:
        documento = json.loads(conteudo)
    except (TypeError, ValueError) as exc:
        raise ValueError("planned manifest is not valid JSON") from exc
    caminho_dados = destino.removesuffix("/_manifesto.json") + "/dados.parquet"
    if (
        not caminho_dados.startswith(prefixo_destino)
        or documento.get("dataset") != dataset
        or documento.get("caminho") != caminho_dados
    ):
        raise ValueError("planned manifest differs from the destination dataset/path")

    recurso_id = _identidade_reparo(destino)
    request_id = f"reparo-manifesto:{recurso_id}"
    depois = _depois_reparo(
        camada=camada,
        dataset=dataset,
        origem=origem,
        destino=destino,
        sha256=sha256,
    )
    esperado_auditoria = {
        "ator_id": ator_id,
        "ator_tipo": str(AtorTipo.USUARIO),
        "acao": str(Acao.EDITAR),
        "antes": None,
        "depois": depois,
        "request_id": request_id,
    }

    await _travar_destino(conn, destino)
    auditorias = await _auditoria_reparo(conn, recurso_id)
    if len(auditorias) > 1:
        raise ValueError(f"repair has more than one canonical audit record: {destino}")
    if auditorias and auditorias[0] != esperado_auditoria:
        raise ValueError(f"existing repair audit record is divergent: {destino}")

    destino_existe = await lake.existe(camada, destino)
    if destino_existe and await lake.ler(camada, destino) != conteudo:
        raise ValueError(f"existing destination manifest is divergent: {destino}")
    if auditorias and not destino_existe:
        raise ValueError(f"repair audit record exists without a destination manifest: {destino}")

    gravou = False
    if not destino_existe:
        try:
            await lake.gravar(camada, destino, conteudo, tipo_mime="application/json")
            gravou = True
        except ErroAplicacao as exc:
            # A writer outside the lock may win the immutable publication. We only reconcile when it
            # published exactly the planned bytes; any divergence remains a hard conflict.
            if exc.codigo != "objeto_ja_existe" or not await lake.existe(camada, destino):
                raise
            if await lake.ler(camada, destino) != conteudo:
                raise ValueError(f"manifest created in a race is divergent: {destino}") from exc

    if not await lake.existe(camada, destino) or await lake.ler(camada, destino) != conteudo:
        raise ValueError(f"manifest does not match after restoration: {destino}")

    if not auditorias:
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo=RECURSO_TIPO,
            recurso_id=recurso_id,
            ator_id=ator_id,
            ator_tipo=AtorTipo.USUARIO,
            antes=None,
            depois=depois,
            request_id=request_id,
        )
    return gravou
