"""Generic service for versioned JSON definitions (draft != published, optimistic lock, audited).
Parameterized by table + versions table + FK column. Used by cube and view."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..authz import permissoes
from ..errors import ErroAplicacao


async def _max_numero(conn: AsyncConnection, versoes: str, idcol: str, rid: uuid.UUID) -> int:
    return int(
        (
            await conn.execute(
                text(f"SELECT coalesce(max(numero), 0) FROM {versoes} WHERE {idcol} = :r"),
                {"r": rid},
            )
        ).scalar_one()
    )


async def _definicao(
    conn: AsyncConnection, versoes: str, idcol: str, rid: uuid.UUID, numero: int
) -> dict | None:
    return (
        await conn.execute(
            text(f"SELECT definicao FROM {versoes} WHERE {idcol} = :r AND numero = :n"),
            {"r": rid, "n": numero},
        )
    ).scalar_one_or_none()


async def criar(
    conn: AsyncConnection,
    *,
    tabela: str,
    recurso_tipo: str,
    nome: str,
    descricao: str | None,
    dono_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> uuid.UUID:
    rid = uuid.uuid4()
    await conn.execute(
        text(f"INSERT INTO {tabela} (id, nome, descricao, dono_id) VALUES (:id, :n, :d, :o)"),
        {"id": rid, "n": nome, "d": descricao, "o": dono_id},
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo=recurso_tipo,
        recurso_id=rid,
        ator_id=dono_id,
        antes=None,
        depois={"nome": nome},
        request_id=request_id,
        ip=ip,
    )
    await permissoes.conceder(
        conn,
        sujeito_tipo="usuario",
        sujeito_id=dono_id,
        recurso_tipo=recurso_tipo,
        recurso_id=rid,
        nivel="administrar",
        concedido_por=dono_id,
    )  # the creator administers
    return rid


async def salvar_rascunho(
    conn: AsyncConnection,
    *,
    versoes: str,
    idcol: str,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    definicao: dict,
    base_numero: int,
    autor_id: uuid.UUID,
    validar: Callable[[dict], None],
    ip=None,
    request_id=None,
) -> int:
    validar(definicao)
    atual = await _max_numero(conn, versoes, idcol, recurso_id)
    if base_numero != atual:
        raise ErroAplicacao(
            codigo="conflito_versao",
            mensagem="The definition changed since you loaded it. Reload.",
            status=409,
        )
    novo = atual + 1
    await conn.execute(
        text(
            f"INSERT INTO {versoes} ({idcol}, numero, definicao, status, autor_id) "
            f"VALUES (:r, :num, CAST(:def AS jsonb), 'rascunho', :a)"
        ),
        {"r": recurso_id, "num": novo, "def": json.dumps(definicao), "a": autor_id},
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo=recurso_tipo,
        recurso_id=recurso_id,
        ator_id=autor_id,
        antes={"numero": atual},
        depois={"numero": novo, "status": "rascunho"},
        request_id=request_id,
        ip=ip,
    )
    return novo


async def publicar(
    conn: AsyncConnection,
    *,
    tabela: str,
    versoes: str,
    idcol: str,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    base_numero: int,
    nota: str | None,
    autor_id: uuid.UUID,
    validar: Callable[[dict], None],
    nome: str | None = None,
    ip=None,
    request_id=None,
) -> int:
    atual = await _max_numero(conn, versoes, idcol, recurso_id)
    if atual == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="There is no definition to publish.", status=400
        )
    if base_numero != atual:
        raise ErroAplicacao(
            codigo="conflito_versao", mensagem="Base version is out of date.", status=409
        )
    definicao = await _definicao(conn, versoes, idcol, recurso_id, atual)
    validar(definicao)
    novo = atual + 1
    vid = uuid.uuid4()
    await conn.execute(
        text(
            f"INSERT INTO {versoes} (id, {idcol}, numero, definicao, status, nota_mudanca, autor_id) "
            f"VALUES (:vid, :r, :num, CAST(:def AS jsonb), 'publicada', :nota, :a)"
        ),
        {
            "vid": vid,
            "r": recurso_id,
            "num": novo,
            "def": json.dumps(definicao),
            "nota": nota,
            "a": autor_id,
        },
    )
    await conn.execute(
        text(f"UPDATE {tabela} SET versao_publicada_id = :vid WHERE id = :r"),
        {"vid": vid, "r": recurso_id},
    )
    depois: dict = {"numero": novo, "nota": nota}
    antes: dict | None = None
    # Optional rename on publish: the resource name is only written here, in the same transaction/audit.
    novo_nome = nome.strip() if nome else None
    if novo_nome:
        nome_atual = (
            await conn.execute(text(f"SELECT nome FROM {tabela} WHERE id = :r"), {"r": recurso_id})
        ).scalar_one()
        if novo_nome != nome_atual:
            await conn.execute(
                text(f"UPDATE {tabela} SET nome = :n WHERE id = :r"),
                {"n": novo_nome, "r": recurso_id},
            )
            antes = {"nome": nome_atual}
            depois["nome"] = novo_nome
    await registrar(
        conn,
        acao=Acao.PUBLICAR,
        recurso_tipo=recurso_tipo,
        recurso_id=recurso_id,
        ator_id=autor_id,
        antes=antes,
        depois=depois,
        request_id=request_id,
        ip=ip,
    )
    return novo


async def excluir(
    conn: AsyncConnection,
    *,
    tabela: str,
    versoes: str,
    idcol: str,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    ator_id: uuid.UUID,
    guardas: list[tuple[str, str, str]] | None = None,
    ip=None,
    request_id=None,
) -> None:
    """Delete the versioned resource and its dependencies (same transaction; audited). 404 if it
    does not exist.

    Order: release the published version, delete the versions, remove the resource permissions,
    and finally the resource itself. No secret in the diff.
    `guardas` = barriers before deleting: `(sql_count, codigo, mensagem)`. If the SELECT count(:r)
    returns > 0, raises 409 (e.g. a page with a stamped report: the stamp is immutable and part
    of the trace chain, so it cannot be cascade-deleted)."""
    nome = (
        await conn.execute(text(f"SELECT nome FROM {tabela} WHERE id = :r"), {"r": recurso_id})
    ).scalar_one_or_none()
    if nome is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Resource not found.", status=404)

    for sql_count, codigo, mensagem in guardas or []:
        n = int((await conn.execute(text(sql_count), {"r": recurso_id})).scalar_one())
        if n > 0:
            raise ErroAplicacao(codigo=codigo, mensagem=mensagem, status=409)

    await conn.execute(
        text(f"UPDATE {tabela} SET versao_publicada_id = NULL WHERE id = :r"), {"r": recurso_id}
    )
    await conn.execute(text(f"DELETE FROM {versoes} WHERE {idcol} = :r"), {"r": recurso_id})
    await conn.execute(
        text("DELETE FROM permissoes WHERE recurso_tipo = :t AND recurso_id = :r"),
        {"t": recurso_tipo, "r": recurso_id},
    )
    await conn.execute(text(f"DELETE FROM {tabela} WHERE id = :r"), {"r": recurso_id})

    await registrar(
        conn,
        acao=Acao.EXCLUIR,
        recurso_tipo=recurso_tipo,
        recurso_id=recurso_id,
        ator_id=ator_id,
        antes={"nome": nome},
        depois=None,
        request_id=request_id,
        ip=ip,
    )


async def renomear(
    conn: AsyncConnection,
    *,
    tabela: str,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nome: str,
    ator_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> dict:
    """Rename a versioned resource (updates ONLY the `nome` column; audited). Does not create a new
    version nor touch the definition. 404 if it does not exist, 400 if the name is empty."""
    novo = nome.strip()
    if not novo:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)
    atual = (
        await conn.execute(text(f"SELECT nome FROM {tabela} WHERE id = :r"), {"r": recurso_id})
    ).scalar_one_or_none()
    if atual is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Resource not found.", status=404)
    if novo != atual:
        await conn.execute(
            text(f"UPDATE {tabela} SET nome = :n WHERE id = :r"), {"n": novo, "r": recurso_id}
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo=recurso_tipo,
            recurso_id=recurso_id,
            ator_id=ator_id,
            antes={"nome": atual},
            depois={"nome": novo},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(recurso_id), "nome": novo}


async def obter(
    conn: AsyncConnection,
    *,
    tabela: str,
    versoes: str,
    idcol: str,
    recurso_id: uuid.UUID,
    numero: int | None = None,
) -> dict:
    """Without `numero` -> the latest version (re-edit); with `numero` -> that specific version
    (view/publish an earlier one). `ultimo` = max number (base of the optimistic lock on
    save/publish)."""
    r = (
        await conn.execute(text(f"SELECT nome FROM {tabela} WHERE id = :r"), {"r": recurso_id})
    ).first()
    if r is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Resource not found.", status=404)
    ultimo = await _max_numero(conn, versoes, idcol, recurso_id)
    alvo = numero if numero is not None else ultimo
    definicao = await _definicao(conn, versoes, idcol, recurso_id, alvo) if alvo else {}
    if numero is not None and definicao is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Version not found.", status=404)
    return {"nome": r.nome, "numero": alvo, "ultimo": ultimo, "definicao": definicao or {}}


async def listar_versoes(
    conn: AsyncConnection, *, tabela: str, versoes: str, idcol: str, recurso_id: uuid.UUID
) -> list[dict]:
    """Versions of the resource (newest first) + status/note/created_at and which one is the active
    PUBLISHED one (table.versao_publicada_id). For the Studio versions dropdown."""
    pub = (
        await conn.execute(
            text(f"SELECT versao_publicada_id FROM {tabela} WHERE id = :r"), {"r": recurso_id}
        )
    ).scalar_one_or_none()
    rows = (
        (
            await conn.execute(
                text(
                    f"SELECT id, numero, status, nota_mudanca, criado_em FROM {versoes} WHERE {idcol} = :r ORDER BY numero DESC"
                ),
                {"r": recurso_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "numero": x["numero"],
            "status": x["status"],
            "nota_mudanca": x["nota_mudanca"],
            "criado_em": x["criado_em"].isoformat() if x["criado_em"] else None,
            "ativa": pub is not None and x["id"] == pub,
        }
        for x in rows
    ]


async def listar(conn: AsyncConnection, tabela: str, *, incluir_sistema: bool = True) -> list[dict]:
    """List {id, nome, sistema}. `incluir_sistema=False` (non-admin) hides flagged items."""
    onde = "" if incluir_sistema else " WHERE NOT sistema"
    rows = (
        (
            await conn.execute(
                text(f"SELECT id, nome, sistema FROM {tabela}{onde} ORDER BY criado_em")
            )
        )
        .mappings()
        .all()
    )
    return [{"id": str(x["id"]), "nome": x["nome"], "sistema": bool(x["sistema"])} for x in rows]


async def definir_sistema(
    conn: AsyncConnection,
    *,
    tabela: str,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    sistema: bool,
    ator_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> dict:
    """Turn the resource `sistema` flag on/off (admin-only at the edge). Silent no-op if it already
    has the requested value; otherwise UPDATE + audit with diff, in the same transaction. 404 if
    it does not exist."""
    atual = (
        await conn.execute(text(f"SELECT sistema FROM {tabela} WHERE id = :r"), {"r": recurso_id})
    ).scalar_one_or_none()
    if atual is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Resource not found.", status=404)
    if bool(atual) != sistema:
        await conn.execute(
            text(f"UPDATE {tabela} SET sistema = :s WHERE id = :r"), {"s": sistema, "r": recurso_id}
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo=recurso_tipo,
            recurso_id=recurso_id,
            ator_id=ator_id,
            antes={"sistema": bool(atual)},
            depois={"sistema": sistema},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(recurso_id), "sistema": sistema}
