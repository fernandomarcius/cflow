"""Pipelines: CRUD, draft (optimistic lock), publish/revert (numbered, audited), diff.

Version model: `pipeline_versoes` is append-only; every save/publish/revert creates a row with
`numero = max+1` (a single sequence per pipeline). Optimistic lock: saving with
`base_numero != max` -> 409.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..authz import permissoes
from ..errors import DetalheErro, ErroAplicacao
from .definicao import validar
from .models import pipeline_versoes, pipelines

# Medallion layers. Mutually exclusive: a pipeline produces ONE layer.
# `None` (no layer) is a legitimate value: it is the plain pipeline, in the General tab.
CAMADAS = ("bronze", "silver", "gold")
# Groups. CLOSED vocabulary: with free text the same thing would become "load",
# "Load" and "LOAD", each spelling opening its own tab in the listing.
GRUPOS = ("load", "reference", "export")


async def criar(
    conn: AsyncConnection,
    *,
    nome: str,
    descricao: str | None,
    dono_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> uuid.UUID:
    pid = uuid.uuid4()
    await conn.execute(
        pipelines.insert(), {"id": pid, "nome": nome, "descricao": descricao, "dono_id": dono_id}
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="pipeline",
        recurso_id=pid,
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
        recurso_tipo="pipeline",
        recurso_id=pid,
        nivel="administrar",
        concedido_por=dono_id,
    )  # the creator administers
    return pid


async def _max_numero(conn: AsyncConnection, pipeline_id: uuid.UUID) -> int:
    n = (
        await conn.execute(
            select(func.max(pipeline_versoes.c.numero)).where(
                pipeline_versoes.c.pipeline_id == pipeline_id
            )
        )
    ).scalar_one()
    return n or 0


async def numero_publicado(conn: AsyncConnection, pipeline_id: uuid.UUID) -> int:
    """Highest PUBLISHED version number (0 if the pipeline was never published).

    `_max_numero` returns the latest number OF ANY KIND, drafts included, which is right for
    re-editing and for the Studio Run button (test before publishing). It does not work for
    resolving the dependency of ANOTHER pipeline: there the rule is that an execution references
    the exact published version, never the latest; otherwise a half-finished draft left open in
    the Studio of an upstream would enter the run of whoever only triggered the consumer."""
    n = (
        await conn.execute(
            select(func.max(pipeline_versoes.c.numero)).where(
                pipeline_versoes.c.pipeline_id == pipeline_id,
                pipeline_versoes.c.status == "publicada",
            )
        )
    ).scalar_one()
    return n or 0


async def _definicao(conn: AsyncConnection, pipeline_id: uuid.UUID, numero: int) -> dict | None:
    return (
        await conn.execute(
            select(pipeline_versoes.c.definicao).where(
                pipeline_versoes.c.pipeline_id == pipeline_id, pipeline_versoes.c.numero == numero
            )
        )
    ).scalar_one_or_none()


async def atualizar_layout(conn: AsyncConnection, *, pipeline_id: uuid.UUID, posicoes: dict) -> int:
    """Persist ONLY the node POSITION (layout) in the current version, IN PLACE, without creating a
    new version or auditing. Moving a card is cosmetic (it changes no logic/SQL/trace), so it
    should not generate a version. `posicoes` = {no_id: {x, y}}. Returns the updated version
    number (0 if there is no version)."""
    numero = await _max_numero(conn, pipeline_id)
    if not numero:
        return 0
    definicao = await _definicao(conn, pipeline_id, numero)
    if not definicao:
        return 0
    for n in definicao.get("nos", []):
        p = posicoes.get(n.get("id"))
        if isinstance(p, dict) and "x" in p and "y" in p:
            n["posicao"] = {"x": p["x"], "y": p["y"]}
    await conn.execute(
        text(
            "UPDATE pipeline_versoes SET definicao = CAST(:d AS jsonb) WHERE pipeline_id = :p AND numero = :n"
        ),
        {"d": json.dumps(definicao), "p": pipeline_id, "n": numero},
    )
    return numero


async def _inserir_versao(
    conn: AsyncConnection,
    pipeline_id: uuid.UUID,
    numero: int,
    definicao: dict,
    status: str,
    autor_id: uuid.UUID,
    nota: str | None = None,
) -> uuid.UUID:
    vid = uuid.uuid4()
    await conn.execute(
        pipeline_versoes.insert(),
        {
            "id": vid,
            "pipeline_id": pipeline_id,
            "numero": numero,
            "definicao": definicao,
            "status": status,
            "nota_mudanca": nota,
            "autor_id": autor_id,
        },
    )
    return vid


async def salvar_rascunho(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    definicao: dict,
    base_numero: int,
    autor_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> int:
    """Save a draft. Optimistic lock: `base_numero` must be the current number; otherwise 409."""
    validar(definicao)  # 422 if the structure is invalid
    atual = await _max_numero(conn, pipeline_id)
    if base_numero != atual:
        raise ErroAplicacao(
            codigo="conflito_versao",
            mensagem="The definition changed since you loaded it. Reload before saving.",
            status=409,
        )
    novo = atual + 1
    await _inserir_versao(conn, pipeline_id, novo, definicao, "rascunho", autor_id)
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="pipeline",
        recurso_id=pipeline_id,
        ator_id=autor_id,
        antes={"numero": atual},
        depois={"numero": novo, "status": "rascunho"},
        request_id=request_id,
        ip=ip,
    )
    return novo


async def validar_conexoes_efeito(conn: AsyncConnection, definicao: dict) -> None:
    """Check the connections of EFFECT nodes (escrita_sql/http/email) AT PUBLISH time (a draft does
    not block): the connection exists, has a compatible type and has `permitir_escrita=true`. 422.

    The structure (conexao_id present, format) was already checked by `validar`; here it is the
    link to the connection's current state: a pipeline that writes to a connection without the
    flag cannot be published."""
    from ..conexoes.conectores import TIPOS_EMAIL, TIPOS_ESCRITA_SQL

    detalhes: list[DetalheErro] = []
    for no in definicao.get("nos", []) if isinstance(definicao, dict) else []:
        tipo = no.get("tipo")
        if tipo not in ("escrita_sql", "http", "email"):
            continue
        nid = no.get("id")
        cid = (no.get("config") or {}).get("conexao_id")
        if not cid:
            continue  # a missing conexao_id was already caught by the structural validation
        try:
            cid_u = uuid.UUID(str(cid))
        except (ValueError, TypeError):
            detalhes.append(DetalheErro(campo="nos", mensagem=f"Node '{nid}': invalid conexao_id."))
            continue
        row = (
            await conn.execute(
                text("SELECT tipo, permitir_escrita FROM conexoes WHERE id = :id"), {"id": cid_u}
            )
        ).first()
        if row is None:
            detalhes.append(
                DetalheErro(campo="nos", mensagem=f"Node '{nid}': connection not found.")
            )
            continue
        if tipo == "escrita_sql" and row.tipo not in TIPOS_ESCRITA_SQL:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Node '{nid}': connection of type '{row.tipo}' cannot write (use postgres/sqlserver).",
                )
            )
        elif tipo == "http" and row.tipo != "api_rest":
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Node '{nid}': connection of type '{row.tipo}' is not api_rest.",
                )
            )
        elif tipo == "email" and row.tipo not in TIPOS_EMAIL:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Node '{nid}': connection of type '{row.tipo}' cannot send e-mail (use smtp).",
                )
            )
        if not row.permitir_escrita:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Node '{nid}': connection is not enabled for writing (permitir_escrita).",
                )
            )

    # `ingestao` node: the link to check is not a connection, it is the registered SOURCE.
    # Without this check, publishing a pipeline that points at a deleted source would only fail
    # at execution, and would fail after being scheduled, when nobody is watching.
    await _validar_fontes_ingestao(conn, definicao, detalhes)

    if detalhes:
        raise ErroAplicacao(
            codigo="conexao_efeito_invalida",
            mensagem=(
                "Some effect nodes have an invalid link: missing connection, incompatible type, "
                "no write permission, or an ingestion source that no longer exists."
            ),
            status=422,
            detalhes=detalhes,
        )


async def _validar_fontes_ingestao(
    conn: AsyncConnection, definicao: dict, detalhes: list[DetalheErro]
) -> None:
    """Check, at PUBLISH time, that every source referenced by an `ingestao` node exists.

    An **inactive** source is not an error: deactivating is how automatic reloading is paused,
    and manual triggering still works. Refusing to publish because of that would force
    reactivating the source just to publish a graph fix."""
    for no in definicao.get("nos", []) if isinstance(definicao, dict) else []:
        if no.get("tipo") != "ingestao":
            continue
        nid = no.get("id")
        fid = (no.get("config") or {}).get("fonte_id")
        if not fid:
            continue  # the structural validation already caught it
        try:
            fid_u = uuid.UUID(str(fid))
        except (ValueError, TypeError):
            detalhes.append(DetalheErro(campo="nos", mensagem=f"Node '{nid}': invalid fonte_id."))
            continue
        existe = (
            await conn.execute(text("SELECT 1 FROM ingestao_fontes WHERE id = :id"), {"id": fid_u})
        ).first()
        if existe is None:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Node '{nid}': ingestion source not found.",
                )
            )


async def publicar(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    base_numero: int,
    nota: str | None,
    autor_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> int:
    """Publish the current definition as a numbered version (validates structure; audits)."""
    atual = await _max_numero(conn, pipeline_id)
    if atual == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="There is no definition to publish.", status=400
        )
    if base_numero != atual:
        raise ErroAplicacao(
            codigo="conflito_versao", mensagem="Base version is out of date.", status=409
        )
    definicao = await _definicao(conn, pipeline_id, atual)
    validar(definicao)
    await validar_conexoes_efeito(conn, definicao)
    novo = atual + 1
    vid = await _inserir_versao(conn, pipeline_id, novo, definicao, "publicada", autor_id, nota)
    await conn.execute(
        pipelines.update().where(pipelines.c.id == pipeline_id).values(versao_publicada_id=vid)
    )
    await registrar(
        conn,
        acao=Acao.PUBLICAR,
        recurso_tipo="pipeline",
        recurso_id=pipeline_id,
        ator_id=autor_id,
        antes=None,
        depois={"numero": novo, "nota": nota},
        request_id=request_id,
        ip=ip,
    )
    return novo


async def reverter(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    para_numero: int,
    autor_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> int:
    """Revert: create a new published version copying the definition of `para_numero` (audits)."""
    definicao = await _definicao(conn, pipeline_id, para_numero)
    if definicao is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Version not found.", status=404)
    novo = await _max_numero(conn, pipeline_id) + 1
    vid = await _inserir_versao(
        conn, pipeline_id, novo, definicao, "publicada", autor_id, f"reverts to v{para_numero}"
    )
    await conn.execute(
        pipelines.update().where(pipelines.c.id == pipeline_id).values(versao_publicada_id=vid)
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="pipeline",
        recurso_id=pipeline_id,
        ator_id=autor_id,
        antes={"revertido_para": para_numero},
        depois={"numero": novo},
        request_id=request_id,
        ip=ip,
    )
    return novo


async def listar_versoes(conn: AsyncConnection, pipeline_id: uuid.UUID) -> list[dict]:
    # Marks which version is the active PUBLISHED one (pipelines.versao_publicada_id). Sorted
    # desc (newest first), includes criado_em and the count of PORTAL executions per version
    # (operator triggered, with pagina_id != NULL; Studio executions have a null pagina_id and
    # do not count here).
    pub = (
        await conn.execute(
            select(pipelines.c.versao_publicada_id).where(pipelines.c.id == pipeline_id)
        )
    ).scalar_one_or_none()
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT v.id, v.numero, v.status, v.nota_mudanca, v.criado_em, "
                    "  (SELECT count(*) FROM execucoes e "
                    "     WHERE e.pipeline_id = v.pipeline_id AND e.versao_numero = v.numero "
                    "       AND e.pagina_id IS NOT NULL) AS exec_portal "
                    "FROM pipeline_versoes v WHERE v.pipeline_id = :p ORDER BY v.numero DESC"
                ),
                {"p": pipeline_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "numero": r["numero"],
            "status": r["status"],
            "nota_mudanca": r["nota_mudanca"],
            "criado_em": r["criado_em"].isoformat() if r["criado_em"] else None,
            "ativa": pub is not None and r["id"] == pub,
            "exec_portal": int(r["exec_portal"]),
        }
        for r in rows
    ]


async def diff(conn: AsyncConnection, pipeline_id: uuid.UUID, a: int, b: int) -> dict:
    return {
        "a": await _definicao(conn, pipeline_id, a),
        "b": await _definicao(conn, pipeline_id, b),
    }


async def listar_pipelines(conn: AsyncConnection, *, incluir_sistema: bool = True) -> list[dict]:
    """List all pipelines with `sistema`, `camada` and `grupo`. The listing does NOT filter by
    either of the last two: the tabs (General/System/Bronze/Silver/Gold/groups) are front-end
    filters over this same response, and an item can appear in three (gold + a group + system).
    The only server-side cut is `sistema`, which is visibility: a non-admin does not receive
    the item, in any tab."""
    q = select(
        pipelines.c.id,
        pipelines.c.nome,
        pipelines.c.sistema,
        pipelines.c.camada,
        pipelines.c.grupo,
        pipelines.c.ativo,
    )
    if not incluir_sistema:
        q = q.where(~pipelines.c.sistema)
    rows = (await conn.execute(q.order_by(pipelines.c.criado_em))).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "nome": r["nome"],
            "sistema": bool(r["sistema"]),
            "camada": r["camada"],
            "grupo": r["grupo"],
            "ativo": bool(r["ativo"]),
        }
        for r in rows
    ]


async def excluir(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Delete the pipeline and everything that depends on it (same transaction; audited). 404 if
    it does not exist.

    Dependency order (real FKs): release the published version, remove schedules, executions
    (and their nodes; `execucao_nos` goes by ON DELETE CASCADE), versions, the resource
    permissions, and finally the pipeline itself. No secret in the diff.
    """
    nome = (
        await conn.execute(text("SELECT nome FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    ).scalar_one_or_none()
    if nome is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)

    await conn.execute(
        text("UPDATE pipelines SET versao_publicada_id = NULL WHERE id = :p"), {"p": pipeline_id}
    )
    await conn.execute(text("DELETE FROM agendamentos WHERE pipeline_id = :p"), {"p": pipeline_id})
    # execucao_nos goes by ON DELETE CASCADE when the executions are deleted
    await conn.execute(text("DELETE FROM execucoes WHERE pipeline_id = :p"), {"p": pipeline_id})
    await conn.execute(
        text("DELETE FROM pipeline_versoes WHERE pipeline_id = :p"), {"p": pipeline_id}
    )
    await conn.execute(
        text("DELETE FROM permissoes WHERE recurso_tipo = 'pipeline' AND recurso_id = :p"),
        {"p": pipeline_id},
    )
    await conn.execute(text("DELETE FROM pipelines WHERE id = :p"), {"p": pipeline_id})

    await registrar(
        conn,
        acao=Acao.EXCLUIR,
        recurso_tipo="pipeline",
        recurso_id=pipeline_id,
        ator_id=ator_id,
        antes={"nome": nome},
        depois=None,
        request_id=request_id,
        ip=ip,
    )


async def renomear(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    nome: str,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Rename the pipeline (updates ONLY the `nome` column; audited). 404 if it does not exist,
    400 if empty.

    Does not touch versions or the definition. No secret in the diff (a name is not a secret)."""
    novo = nome.strip()
    if not novo:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)
    atual = (
        await conn.execute(text("SELECT nome FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    ).scalar_one_or_none()
    if atual is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)
    if novo != atual:
        await conn.execute(
            text("UPDATE pipelines SET nome = :n WHERE id = :p"), {"n": novo, "p": pipeline_id}
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="pipeline",
            recurso_id=pipeline_id,
            ator_id=ator_id,
            antes={"nome": atual},
            depois={"nome": novo},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(pipeline_id), "nome": novo}


async def definir_camada(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    camada: str | None,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Set the pipeline's medallion layer (admin-only at the edge). `None` removes the layer.

    Mirrors `versionado.definir_sistema`: silent no-op when it already has the requested value;
    otherwise UPDATE + audit with diff, in the SAME transaction. 404 if the pipeline does not
    exist, 422 if the value is not a known layer.

    It lives here and not in `versionado.py` on purpose: `definir_sistema` is generic because
    every entity has the `sistema` column; `camada` exists ONLY in `pipelines`. A generic
    function there would build `UPDATE {table} SET camada` for cubes/views, which lack the
    column.

    Does not touch `sistema`: layer and visibility are independent by contract.
    """
    if camada is not None and camada not in CAMADAS:
        raise ErroAplicacao(
            codigo="camada_invalida",
            mensagem="Invalid layer. Use bronze, silver or gold, or none.",
            status=422,
            detalhes=[DetalheErro(campo="camada", mensagem=f"Unrecognized value: {camada!r}.")],
        )
    # `.first()`, not `.scalar_one_or_none()`: `camada` is nullable, and the scalar would return
    # None both for "pipeline does not exist" and "exists with no layer", giving a wrong 404.
    linha = (
        await conn.execute(text("SELECT camada FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    ).first()
    if linha is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)
    atual = linha[0]
    if atual != camada:
        await conn.execute(
            text("UPDATE pipelines SET camada = :c WHERE id = :p"),
            {"c": camada, "p": pipeline_id},
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="pipeline",
            recurso_id=pipeline_id,
            ator_id=ator_id,
            antes={"camada": atual},
            depois={"camada": camada},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(pipeline_id), "camada": camada}


async def definir_grupo(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    grupo: str | None,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Set the pipeline's group (admin-only at the edge). `None` removes it.

    Twin of `definir_camada` and deliberately SEPARATE from it: the two columns coexist on the
    same pipeline (one can be `gold` AND `load`), so a function that set "the classification"
    would have to choose which one to clear. Silent no-op when it already has the requested
    value.
    """
    if grupo is not None and grupo not in GRUPOS:
        raise ErroAplicacao(
            codigo="grupo_invalido",
            mensagem=f"Invalid group. Use {', '.join(GRUPOS)}, or none.",
            status=422,
            detalhes=[DetalheErro(campo="grupo", mensagem=f"Unrecognized value: {grupo!r}.")],
        )
    # `.first()` and not `.scalar_one_or_none()`: `grupo` is nullable and the scalar cannot tell
    # "pipeline does not exist" from "exists with no group", giving a wrong 404. Same as layer.
    linha = (
        await conn.execute(text("SELECT grupo FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    ).first()
    if linha is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)
    atual = linha[0]
    if atual != grupo:
        await conn.execute(
            text("UPDATE pipelines SET grupo = :g WHERE id = :p"),
            {"g": grupo, "p": pipeline_id},
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="pipeline",
            recurso_id=pipeline_id,
            ator_id=ator_id,
            antes={"grupo": atual},
            depois={"grupo": grupo},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(pipeline_id), "grupo": grupo}


async def definir_ativo(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    ativo: bool,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Activate/deactivate the pipeline (admin-only at the edge). Does not block
    editing/publishing, only EXECUTION, checked at the single choke point
    (`execucao.engine.criar_execucao`), so it holds on every trigger (UI, cron scheduler,
    re-run), not only on the `/executar` route.

    Silent no-op when it already has the requested value; otherwise UPDATE + audit with diff, in
    the SAME transaction. 404 if the pipeline does not exist. Plain boolean: it does not use
    `.first()` like `definir_camada`/`definir_grupo`, because `ativo` is not nullable, so
    `scalar_one_or_none()` tells "does not exist" (`None`) from "exists" unambiguously.
    """
    atual = (
        await conn.execute(text("SELECT ativo FROM pipelines WHERE id = :p"), {"p": pipeline_id})
    ).scalar_one_or_none()
    if atual is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)
    if bool(atual) != ativo:
        await conn.execute(
            text("UPDATE pipelines SET ativo = :a WHERE id = :p"),
            {"a": ativo, "p": pipeline_id},
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="pipeline",
            recurso_id=pipeline_id,
            ator_id=ator_id,
            antes={"ativo": bool(atual)},
            depois={"ativo": ativo},
            request_id=request_id,
            ip=ip,
        )
    return {"id": str(pipeline_id), "ativo": ativo}


async def obter(conn: AsyncConnection, pipeline_id: uuid.UUID, numero: int | None = None) -> dict:
    """Open a pipeline: name + number + definition + `ativo`. Without `numero` -> the latest
    version (to re-edit); with `numero` -> that specific version (to view/run/publish an earlier
    one). 404 if the requested version does not exist; `ultimo` reports the max number (base of
    the optimistic lock on save)."""
    p = (
        await conn.execute(
            select(pipelines.c.nome, pipelines.c.ativo).where(pipelines.c.id == pipeline_id)
        )
    ).first()
    if p is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Pipeline not found.", status=404)
    ultimo = await _max_numero(conn, pipeline_id)
    alvo = numero if numero is not None else ultimo
    definicao = await _definicao(conn, pipeline_id, alvo) if alvo else {"nos": [], "arestas": []}
    if numero is not None and definicao is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Version not found.", status=404)
    return {
        "nome": p.nome,
        "numero": alvo,
        "ultimo": ultimo,
        "definicao": definicao or {"nos": [], "arestas": []},
        "ativo": bool(p.ativo),
    }
