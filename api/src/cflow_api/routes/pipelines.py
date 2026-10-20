"""Studio routes: pipeline definition, draft (optimistic lock), publishing, per-node preview.

Editing a pipeline requires specialist/admin at the edge (a simplification; fine-grained
per-`pipeline` permission comes in once there is a grant UI). Publish/revert are audited.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import agendador, reexecucao
from cflow_core.execucao import engine as execucao
from cflow_core.modelagem import versionado
from cflow_core.pipelines import derivacao, preview, service
from cflow_core.request_context import request_id_atual
from cflow_core.timezone import FUSO_SAO_PAULO

from ..deps import exigir_perfil, usuario_atual

router = APIRouter()
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
# Whoever TRIGGERS and FOLLOWS without authoring: the service account enters here and only here;
# the authoring routes stay on `Editor`. It reaches `executar` on EVERY pipeline without a grant
# (`permissoes.ALCANCE_DO_SERVICO`); the other profiles stay on fine-grained permission.
Executor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN, Perfil.SERVICO))
# Any authenticated user (no profile gate). Used on the Portal path.
Logado = Depends(usuario_atual)


async def _autorizar_execucao(conn, ator: uuid.UUID, pipeline_id: uuid.UUID, pagina_id) -> None:
    """Authorize triggering an execution. Two paths:
    - **Portal** (with `pagina_id`): any authenticated user can GENERATE A REPORT from a PUBLISHED
      catalog page, as long as the page references THIS pipeline (scope). No fine-grained
      permission: a published page is, by construction, consumable by everyone (read-only Portal).
    - **Studio / integration** (no `pagina_id`): requires specialist/admin + the `executar`
      permission on the pipeline, or a service account, which reaches `executar` on every
      pipeline. Without it here, the facade got a 403 when asking about the day's load
      (`execucoes-do-dia`).
    """
    if pagina_id is not None:
        row = (
            await conn.execute(
                text(
                    "SELECT definicao->'execucao'->>'pipeline_id' AS pid FROM pagina_versoes "
                    "WHERE pagina_id = :pg AND status = 'publicada' ORDER BY numero DESC LIMIT 1"
                ),
                {"pg": pagina_id},
            )
        ).first()
        if row is not None and row.pid and str(row.pid) == str(pipeline_id):
            return  # the published page references this pipeline: open to any authenticated user
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)
    # Studio: profile + fine-grained permission
    perfil = (
        await conn.execute(
            text("SELECT perfil FROM usuarios WHERE id = :id AND ativo = true"), {"id": ator}
        )
    ).scalar_one_or_none()
    if perfil not in (Perfil.ESPECIALISTA, Perfil.ADMIN, Perfil.SERVICO):
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)
    await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")


async def _autorizar_leitura_execucao(conn, ator: uuid.UUID, execucao_id: uuid.UUID) -> None:
    """Authorize READING the state of an execution. Two paths, symmetric to `_autorizar_execucao`:
    - **Portal:** an execution born from a page (non-null `pagina_id`) or triggered by this same
      user; this is the "Generate report" poll. Whoever can generate must be able to follow.
    - **Studio:** specialist/admin for any execution.
    - **Service account:** an execution by ANOTHER actor, of any pipeline (it reaches them all,
      `permissoes.ALCANCE_DO_SERVICO`). The adapter's deduplication re-finds the run another
      delivery triggered, and the 409 points at the in-flight execution of whoever it is;
      without read access the task would go to the DLQ while the run is still going.

    Applies to the execution STATE. A node's raw data is more sensitive and has its own, narrower
    guard (see `preview_no_execucao`).
    """
    row = (
        await conn.execute(
            text("SELECT iniciada_por, pagina_id, pipeline_id FROM execucoes WHERE id = :id"),
            {"id": execucao_id},
        )
    ).first()
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Execution not found.", status=404)
    if row.pagina_id is not None or (
        row.iniciada_por is not None and str(row.iniciada_por) == str(ator)
    ):
        return
    perfil = (
        await conn.execute(
            text("SELECT perfil FROM usuarios WHERE id = :id AND ativo = true"), {"id": ator}
        )
    ).scalar_one_or_none()
    if perfil == Perfil.SERVICO:
        await permissoes.exigir(conn, ator, "pipeline", row.pipeline_id, "executar")
        return
    if perfil not in (Perfil.ESPECIALISTA, Perfil.ADMIN):
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)


async def _eh_servico(conn, ator: uuid.UUID) -> bool:
    """Is the account a SERVICE account?

    Compares as TEXT: the `servico` label only exists in the enum after a later migration, and
    comparing the enum directly against the literal makes Postgres read the literal as an enum
    value; on a database without it, EVERY pipeline and execution listing would answer 500, for
    any user. Keep the comparison as text."""
    return (
        await conn.execute(
            text("SELECT perfil::text = 'servico' FROM usuarios WHERE id = :id AND ativo"),
            {"id": ator},
        )
    ).scalar_one_or_none() is True


class CriarPipelineIn(BaseModel):
    nome: str
    descricao: str | None = None


class RenomearIn(BaseModel):
    nome: str


class RascunhoIn(BaseModel):
    definicao: dict
    base_numero: int


class PublicarIn(BaseModel):
    base_numero: int
    nota: str | None = None


class ReverterIn(BaseModel):
    para_numero: int


class PreviewIn(BaseModel):
    definicao: dict
    parametros: dict | None = (
        None  # values to bind the :params in the preview (lenient: uses the default)
    )


class ExecutarIn(BaseModel):
    parametros: dict | None = None  # values of the declared runtime parameters (unit/month/...)
    pagina_id: uuid.UUID | None = (
        None  # page that originated the run (Portal); null = triggered from the Studio
    )
    versao_numero: int | None = (
        None  # runs a SPECIFIC version (without creating another); null = the latest
    )


class ReexecutarIn(BaseModel):
    desde_no: str | None = None  # redo this node and what depends on it; null = the whole execution
    # ONLY to restore `segredo: true` parameters, whose persisted value is `***` (redacted).
    # Re-running with other parameters is not re-running; that is what the Run button is for.
    parametros: dict | None = None


class ExecucoesDoDiaIn(BaseModel):
    parametros: dict | None = None  # EQUALITY: the same parameters that would be executed
    # CONTAINMENT: only the keys that are known. It is the question "is there a load from today for
    # unit X?", whose as-of is what we want to find out; asking for it in the question would be
    # asking for the answer.
    parametros_contidos: dict | None = None
    # OPTIONAL: with a page, the cache is the Portal's (per page + pipeline + parameters + day);
    # without one, the response covers what was triggered by API and by the scheduler, where
    # `pagina_id` is NULL, which is where the nightly load comes from. Without a page,
    # `_autorizar_execucao` falls to the Studio path and requires profile + `executar`
    # permission; the edge does not loosen.
    pagina_id: uuid.UUID | None = None


class SistemaIn(BaseModel):
    sistema: bool


class CamadaIn(BaseModel):
    # `str | None`, not `Literal[...]`: an invalid value must come out in the error envelope
    # (`camada_invalida`, validated in the service), and not in FastAPI's default 422, which has a
    # different shape and falls outside the envelope; the API has no RequestValidationError handler.
    camada: str | None = None


class GrupoIn(BaseModel):
    # Same reason as CamadaIn for not using `Literal[...]`: the error must come out in the standard
    # envelope.
    grupo: str | None = None


class AtivoIn(BaseModel):
    ativo: bool


class OpcoesNoIn(BaseModel):
    # Options of an operator dropdown from the OUTPUT of a pipeline node (parallel to
    # /conexoes/{id}/opcoes, but the source is a node): the value sent + the label shown (nickname).
    no_id: str
    coluna_valor: str  # VALUE column (what the param receives)
    coluna_rotulo: str | None = None  # LABEL column shown; absent: same as the value
    parametros: dict | None = None  # values to bind :params when materializing the node (lenient)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/pipelines", status_code=201)
async def criar_pipeline(
    dados: CriarPipelineIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        pid = await service.criar(
            conn,
            nome=dados.nome,
            descricao=dados.descricao,
            dono_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(pid), "numero": 0}


@router.get("/pipelines")
async def listar_pipelines(ator: uuid.UUID = Executor) -> list[dict]:
    async with transacao() as conn:
        # Service account: sees the whole catalog, `sistema` included. It reaches every pipeline
        # (`permissoes.ALCANCE_DO_SERVICO`), and it is through this list that the integrator
        # resolves a pipeline by name.
        incluir = await sistema.ve_sistema(conn, ator, "pipeline")
        return await service.listar_pipelines(conn, incluir_sistema=incluir)


@router.get("/pipelines/{pipeline_id}")
async def obter_pipeline(
    pipeline_id: uuid.UUID, numero: int | None = None, ator: uuid.UUID = Editor
) -> dict:
    """`?numero=N` opens that specific version; without it, the latest (default)."""
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await service.obter(conn, pipeline_id, numero)


@router.get("/pipelines/{pipeline_id}/cadeia")
async def plano_cadeia_pipeline(pipeline_id: uuid.UUID, ator: uuid.UUID = Editor) -> dict:
    """What a trigger of this pipeline runs TOGETHER, without triggering: the whole upstream, its
    external effects, what it publishes and the refusal the trigger would give. It is what the
    scheduling screen shows before the click: scheduling a consumer runs the upstreams with all
    their effects, and that must not be discovered afterwards, in the trail."""
    from cflow_core.execucao import disparo

    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await disparo.plano_cadeia(conn, pipeline_id)


@router.patch("/pipelines/{pipeline_id}/sistema")
async def definir_sistema_pipeline(
    pipeline_id: uuid.UUID, dados: SistemaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    async with transacao() as conn:
        return await versionado.definir_sistema(
            conn,
            tabela="pipelines",
            recurso_tipo="pipeline",
            recurso_id=pipeline_id,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.patch("/pipelines/{pipeline_id}/camada", status_code=204)
async def definir_camada_pipeline(
    pipeline_id: uuid.UUID, dados: CamadaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    """Set the pipeline's medallion layer (`bronze`|`silver`|`gold`; `null` removes it). Admin-only,
    audited in the same transaction. Does NOT touch `sistema`: the layer is a tab, `sistema` is visibility."""
    async with transacao() as conn:
        await service.definir_camada(
            conn,
            pipeline_id=pipeline_id,
            camada=dados.camada,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/pipelines/{pipeline_id}/grupo", status_code=204)
async def definir_grupo_pipeline(
    pipeline_id: uuid.UUID, dados: GrupoIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    """Set the pipeline GROUP (`load`, `reference` or `export`; `null` removes it). Admin-only,
    audited in the same transaction. Independent of `camada` and `sistema`: the same pipeline can
    have all three."""
    async with transacao() as conn:
        await service.definir_grupo(
            conn,
            pipeline_id=pipeline_id,
            grupo=dados.grupo,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/pipelines/{pipeline_id}/ativo", status_code=204)
async def definir_ativo_pipeline(
    pipeline_id: uuid.UUID, dados: AtivoIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    """Activate/deactivate the pipeline (admin-only, audited in the same transaction). Deactivated
    blocks EXECUTION on every trigger (UI, cron scheduler, re-run); the check lives in the
    engine, not here. It does NOT prevent editing/publishing a draft: only running."""
    async with transacao() as conn:
        await service.definir_ativo(
            conn,
            pipeline_id=pipeline_id,
            ativo=dados.ativo,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.delete("/pipelines/{pipeline_id}", status_code=204)
async def excluir_pipeline(
    pipeline_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor
) -> Response:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "administrar")
        await service.excluir(
            conn,
            pipeline_id=pipeline_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/pipelines/{pipeline_id}")
async def renomear_pipeline(
    pipeline_id: uuid.UUID, dados: RenomearIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "administrar")
        return await service.renomear(
            conn,
            pipeline_id=pipeline_id,
            nome=dados.nome,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.put("/pipelines/{pipeline_id}/rascunho")
async def salvar_rascunho(
    pipeline_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "editar")
        numero = await service.salvar_rascunho(
            conn,
            pipeline_id=pipeline_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/pipelines/{pipeline_id}/publicar", status_code=201)
async def publicar(
    pipeline_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "editar")
        numero = await service.publicar(
            conn,
            pipeline_id=pipeline_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            autor_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/pipelines/{pipeline_id}/reverter", status_code=201)
async def reverter(
    pipeline_id: uuid.UUID, dados: ReverterIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "editar")
        numero = await service.reverter(
            conn,
            pipeline_id=pipeline_id,
            para_numero=dados.para_numero,
            autor_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


class LayoutIn(BaseModel):
    posicoes: dict  # { no_id: { x, y } }: card positions only


@router.patch("/pipelines/{pipeline_id}/layout", status_code=204)
async def atualizar_layout(
    pipeline_id: uuid.UUID, dados: LayoutIn, ator: uuid.UUID = Editor
) -> Response:
    """Persist only the node positions in the current version (in place); moving a card creates no version."""
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "editar")
        await service.atualizar_layout(conn, pipeline_id=pipeline_id, posicoes=dados.posicoes)
    return Response(status_code=204)


@router.get("/pipelines/{pipeline_id}/versoes")
async def versoes(pipeline_id: uuid.UUID, ator: uuid.UUID = Editor) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await service.listar_versoes(conn, pipeline_id)


@router.post("/pipelines/{pipeline_id}/nos/{no_id}/preview")
async def preview_no(
    pipeline_id: uuid.UUID, no_id: str, dados: PreviewIn, ator: uuid.UUID = Editor
) -> dict:
    chave = f"{ator}:{pipeline_id}"  # preview session per user+pipeline
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await preview.preview_no(
            conn, chave=chave, definicao=dados.definicao, no_id=no_id, parametros=dados.parametros
        )


@router.post("/pipelines/{pipeline_id}/opcoes-no")
async def opcoes_no(
    pipeline_id: uuid.UUID, dados: OpcoesNoIn, ator: uuid.UUID = Logado
) -> list[dict]:
    """Dynamic options of a dropdown from the OUTPUT of a pipeline node (latest version):
    DISTINCT, sorted `[{valor, rotulo}]`. Parallel to /conexoes/{id}/opcoes; only the origin
    changes (node vs connection). Open to any authenticated user (the operator needs it to build
    the filter in the Portal, as with the connection endpoint). Read-only; effect/code nodes are
    refused at materialization."""
    async with transacao() as conn:  # definition only; materialization reads the source outside
        det = await service.obter(conn, pipeline_id, None)
    return await derivacao.opcoes_de_no(
        det["definicao"],
        dados.no_id,
        dados.coluna_valor,
        dados.coluna_rotulo,
        dados.parametros,
    )


@router.post("/pipelines/{pipeline_id}/nos/{no_id}/colunas-disponiveis")
async def colunas_disponiveis(
    pipeline_id: uuid.UUID, no_id: str, dados: PreviewIn, ator: uuid.UUID = Editor
) -> dict:
    chave = f"{ator}:{pipeline_id}"
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await preview.colunas_disponiveis(
            conn, chave=chave, definicao=dados.definicao, no_id=no_id
        )


async def _executar_dag(
    *,
    pipeline_id: uuid.UUID,
    request: Request,
    ator: uuid.UUID,
    parametros: dict | None,
    pagina_id: uuid.UUID | None,
    versao_numero: int | None,
) -> dict:
    """Resolve and execute the pipeline WITH ITS DEPENDENCIES (pipeline DAG): runs the upstreams
    (`pipeline` sources) in topological order and the root reads the EXACT snapshots just
    produced (freshness). Assumes permission was ALREADY checked by the caller. Shared by
    `/executar` (when the pipeline has dependencies) and `/executar-encadeado`.

    The same `{parametros}` flow to the whole chain (each pipeline validates only the ones it
    declares). With Temporal, pre-creates the chain's executions and starts the ExecutarDAG
    workflow (asynchronous), returning the chain as 'executando'; without Temporal, runs inline and
    returns the root's result.
    """
    from cflow_core.config import get_settings
    from cflow_core.execucao.disparo import abortar, preparar_cadeia
    from cflow_core.pipelines.dag import resolver_ordem

    cfg = get_settings()
    if cfg.usar_temporal:
        # Pre-create ONE execution per pipeline in the order -> a complete freshness map BEFORE
        # running. The pre-creation lives in `cflow_core.execucao.disparo` because the WORKER (which
        # cannot import `cflow_api`) needs exactly the same for chained scheduling; two copies would
        # make the screen and the night run diverge.
        async with transacao() as conn:
            ordem = await resolver_ordem(conn, pipeline_id, versao_numero)
        pronto = await preparar_cadeia(
            ordem=ordem,
            raiz_id=pipeline_id,
            ator_id=ator,
            parametros=parametros,
            pagina_id=pagina_id,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
        from temporalio.client import Client

        raiz_exec = pronto["raiz"]
        try:
            cliente = await Client.connect(cfg.temporal_host)
            await cliente.start_workflow(
                "ExecutarDAG",
                args=[pronto["cadeia"], pronto["fontes_execucao"], parametros],
                id=f"dag-{raiz_exec}",
                task_queue=cfg.temporal_task_queue,
            )
        except Exception:
            # The WHOLE chain was already pre-created (the freshness map must be complete before any
            # node runs), and between the pre-creation and Temporal accepting the workflow there is a
            # gap: full queue, dropped client, database blinking. Without this, the executions stay
            # 'executando' FOREVER: the `try/except` of `ExecutarDAG` cannot reach it because the
            # workflow never started, and the panel shows an execution in progress that nobody is
            # running. Scheduled triggering already closed this hole; here it is the Studio Run
            # button, which is where someone is watching the screen waiting for an answer.
            await abortar(
                [c["execucao_id"] for c in pronto["cadeia"]],
                "Could not start the execution.",
            )
            raise
        return {
            "id": raiz_exec,
            "status": "executando",
            "cadeia": [
                {"pipeline_id": c["pipeline_id"], "execucao_id": c["execucao_id"]}
                for c in pronto["cadeia"]
            ],
        }

    return await execucao.executar_encadeado(
        pipeline_id=pipeline_id,
        ator_id=ator,
        parametros=parametros,
        pagina_id=pagina_id,
        versao_numero=versao_numero,
        ip=_ip(request),
        request_id=request_id_atual(),
    )


@router.post("/pipelines/{pipeline_id}/executar")
async def executar(
    pipeline_id: uuid.UUID,
    request: Request,
    dados: ExecutarIn | None = Body(None),
    ator: uuid.UUID = Logado,
) -> dict:
    """Execute the current graph over the FULL data; Parquet snapshot + per-node trail.

    Accepts `{parametros}` (values of the declared runtime parameters); validated against the
    definition at the edge (undeclared/missing required -> 400). The body is optional.

    Authorization: via the Portal (with the `pagina_id` of a published page) any authenticated
    user generates a report; without `pagina_id` it requires specialist/admin + the `executar`
    permission, or a service account.

    With Temporal configured, starts the workflow (asynchronous) and returns the execution as
    'executando'; the client follows it through GET /execucoes/{id}. Without Temporal, runs inline
    and returns the result.

    If the pipeline has a `pipeline` source (pipeline DAG), it resolves and executes the
    dependencies TOO, right now (same mechanism as `/executar-encadeado`); it never reads "the
    latest execution of any kind" of the source pipeline: without this, a parameterized
    pipeline source would leak data from ANOTHER unit whenever executed outside an explicit
    chain. Without dependencies, behavior is identical to before (no overhead).
    """
    from cflow_core.config import get_settings
    from cflow_core.pipelines.dag import resolver_ordem

    parametros = dados.parametros if dados else None
    pagina_id = dados.pagina_id if dados else None
    versao_numero = dados.versao_numero if dados else None
    async with transacao() as conn:
        await _autorizar_execucao(conn, ator, pipeline_id, pagina_id)
        ordem = await resolver_ordem(conn, pipeline_id, versao_numero)
    if len(ordem) > 1:
        return await _executar_dag(
            pipeline_id=pipeline_id,
            request=request,
            ator=ator,
            parametros=parametros,
            pagina_id=pagina_id,
            versao_numero=versao_numero,
        )
    cfg = get_settings()
    if cfg.usar_temporal:
        ctx = await execucao.criar_execucao(
            pipeline_id=pipeline_id,
            ator_id=ator,
            parametros=parametros,
            pagina_id=pagina_id,
            versao_numero=versao_numero,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
        from temporalio.client import Client

        cliente = await Client.connect(cfg.temporal_host)
        await cliente.start_workflow(
            "ExecutarPipeline",
            args=[ctx["execucao_id"], ctx["definicao"], ctx["ordem"], ctx["parametros"]],
            id=f"exec-{ctx['execucao_id']}",
            task_queue=cfg.temporal_task_queue,
        )
        return {"id": ctx["execucao_id"], "status": "executando", "versao": ctx["versao"]}

    try:
        return await execucao.executar(
            pipeline_id=pipeline_id,
            ator_id=ator,
            parametros=parametros,
            pagina_id=pagina_id,
            versao_numero=versao_numero,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    except ErroAplicacao as exc:
        # The execution ALREADY EXISTS (it is created before running): a node that fails is a
        # BUSINESS outcome, not a request error. Returning only the 400 envelope left the caller
        # without the id (no path to the detail or to the trail itself), and made the SAME route
        # answer in two formats depending on the mode: with Temporal it always returns 200 + id and
        # the failure shows up in GET /execucoes/{id}. The envelope still applies to what fails
        # BEFORE there is an execution (no definition, invalid parameter, permission): no id there.
        exec_id = getattr(exc, "execucao_id", None)
        if exec_id is None:
            raise
        return {"id": exec_id, "status": "falha", "erro": exc.mensagem}


@router.post("/pipelines/{pipeline_id}/executar-encadeado")
async def executar_encadeado(
    pipeline_id: uuid.UUID,
    request: Request,
    dados: ExecutarIn | None = Body(None),
    ator: uuid.UUID = Executor,
) -> dict:
    """Execute the pipeline WITH ITS DEPENDENCIES (pipeline DAG): runs the upstreams (`pipeline`
    sources) in topological order and the root reads the EXACT snapshots just produced (freshness).

    The same `{parametros}` flow to the whole chain (each pipeline validates only the ones it
    declares). With Temporal, pre-creates the chain's executions and starts the ExecutarDAG
    workflow (asynchronous), returning the chain as 'executando'; without Temporal, runs inline and
    returns the root's result.
    """
    parametros = dados.parametros if dados else None
    pagina_id = dados.pagina_id if dados else None
    versao_numero = dados.versao_numero if dados else None
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")
    return await _executar_dag(
        pipeline_id=pipeline_id,
        request=request,
        ator=ator,
        parametros=parametros,
        pagina_id=pagina_id,
        versao_numero=versao_numero,
    )


class ParametrosIn(BaseModel):
    parametros: dict | None = None
    versao_numero: int | None = None


@router.post("/pipelines/{pipeline_id}/parametros/normalizados")
async def parametros_normalizados(
    pipeline_id: uuid.UUID, dados: ParametrosIn | None = Body(None), ator: uuid.UUID = Executor
) -> dict:
    """The parameters as the root execution would STORE them, without executing.

    `{parametros, versao, fora_da_raiz, relativos}`. It is the same normalization as the trigger,
    so whoever needs to recognize an execution by its parameters (an integration adapter, on
    redelivery) asks here instead of reimplementing it. Same permission as executing: whoever asks
    is whoever will trigger. Nothing is written, so there is no trail."""
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")
    return await execucao.parametros_como_guardados(
        pipeline_id,
        dados.parametros if dados else None,
        versao_numero=dados.versao_numero if dados else None,
    )


@router.post("/pipelines/{pipeline_id}/execucoes-do-dia")
async def execucoes_do_dia(
    pipeline_id: uuid.UUID, dados: ExecucoesDoDiaIn, ator: uuid.UUID = Logado
) -> list[dict]:
    """COMPLETED executions from today (display time zone): the day's cache (manual or scheduled).

    With `parametros`, matches by EQUALITY: it is what the Portal uses for 'see existing / history
    / generate new' before re-executing. With `parametros_contidos`, matches by CONTAINMENT: it is
    the nightly-load question ("is there a load from today for this unit?"), where the as-of is
    the answer and not the question. Both together are refused."""
    async with transacao() as conn:
        await _autorizar_execucao(conn, ator, pipeline_id, dados.pagina_id)
        return await execucao.execucoes_do_dia(
            conn,
            pipeline_id=pipeline_id,
            pagina_id=dados.pagina_id,
            parametros=dados.parametros,
            parametros_contidos=dados.parametros_contidos,
        )


@router.get("/pipelines/{pipeline_id}/execucoes")
async def listar_execucoes(
    pipeline_id: uuid.UUID, limite: int = 25, offset: int = 0, ator: uuid.UUID = Editor
) -> dict:
    """Paginated: `{itens, total, limite, offset}` (most recent first), for the history."""
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await execucao.listar(conn, pipeline_id, limite=limite, offset=offset)


@router.get("/execucoes")
async def listar_execucoes_global(
    ator: uuid.UUID = Executor,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(executando|concluida|falha)$"),
    origem: str | None = Query(default=None, pattern="^(manual|agendado|reexecucao)$"),
    de: datetime | None = None,
    ate: datetime | None = None,
    busca: str | None = Query(default=None, max_length=200),
    aba: str | None = Query(
        default=None,
        pattern="^(geral|sistema|bronze|silver|gold|load|reference|export)$",
    ),
    limite: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Execution history of ALL pipelines (or of one, with `?pipeline_id=`), filterable: the Studio
    Executions screen.

    Combinable filters: `status`, `origem`, the window `[de, ate)` over the execution start
    (**`ate` is EXCLUSIVE**), `busca` (free text: pipeline, schedule, page, who triggered, error,
    parameters, id; `key=value` matches an exact parameter) and `aba` (the pipeline's
    classification, same contract as the tabs of the pipelines listing). `totais_abas` counts the
    executions of each tab with the other filters applied. A moment without a time zone is read as
    the display time zone. An execution of a pipeline flagged `sistema` only appears for admins
    (it enters neither the items nor the `total`).
    """
    if de is not None and de.tzinfo is None:
        de = de.replace(tzinfo=FUSO_SAO_PAULO)
    if ate is not None and ate.tzinfo is None:
        ate = ate.replace(tzinfo=FUSO_SAO_PAULO)
    async with transacao() as conn:
        if await _eh_servico(conn, ator):
            # Service account: the executions of ONE pipeline at a time; the general listing is the
            # whole house, and the integrator always asks about one.
            if pipeline_id is None:
                raise ErroAplicacao(
                    codigo="acesso_negado",
                    mensagem="A service account lists the executions of one pipeline at a time.",
                    status=403,
                )
            await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")
        if pipeline_id is not None:
            await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await execucao.listar_global(
            conn,
            pipeline_id=pipeline_id,
            status=status,
            origem=origem,
            de=de,
            ate=ate,
            busca=busca,
            aba=aba,
            limite=limite,
            offset=offset,
            incluir_sistema=await sistema.ve_sistema(conn, ator, "pipeline"),
        )


@router.get("/corridas/{cadeia_id}")
async def mapa_da_corrida(cadeia_id: uuid.UUID, ator: uuid.UUID = Editor) -> dict:
    """A run (pipeline chain) as a MAP: the executions and who depends on whom.

    Same permission and visibility as `GET /execucoes`: an execution of a `sistema` pipeline only
    appears for admins. A run that does not exist or has nothing visible -> 404
    `corrida_inexistente`. Read-only.
    """
    async with transacao() as conn:
        return await execucao.consultar_corrida(
            conn, cadeia_id, incluir_sistema=await sistema.eh_admin(conn, ator)
        )


@router.get("/pipelines/{pipeline_id}/execucoes-resumo")
async def resumo_execucoes(
    pipeline_id: uuid.UUID, agendamento_id: uuid.UUID | None = None, ator: uuid.UUID = Editor
) -> dict:
    """History summarized per day (`{dias, total}`). With `?agendamento_id=` it is scoped to the
    executions of that schedule only (History tab of the detail)."""
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await execucao.resumo_execucoes(conn, pipeline_id, agendamento_id=agendamento_id)


@router.get("/agendamentos/{agendamento_id}/execucoes")
async def execucoes_do_agendamento(
    agendamento_id: uuid.UUID, dia: str | None = None, limite: int = 200, ator: uuid.UUID = Editor
) -> list[dict]:
    """Executions of ONE schedule (each run), ordered by time desc; `?dia=YYYY-MM-DD` filters the
    day (History drill: see the 1st, 2nd, 3rd... that ran)."""
    async with transacao() as conn:
        return await execucao.listar_por_agendamento(conn, agendamento_id, dia=dia, limite=limite)


@router.get("/execucoes/{execucao_id}")
async def consultar_execucao(execucao_id: uuid.UUID, ator: uuid.UUID = Logado) -> dict:
    """State + per-node trail. See `_autorizar_leitura_execucao` for who can read."""
    async with transacao() as conn:
        await _autorizar_leitura_execucao(conn, ator, execucao_id)
        return await execucao.consultar(conn, execucao_id)


async def _autorizar_reexecucao(conn, ator: uuid.UUID, execucao_id: uuid.UUID) -> uuid.UUID:
    """Re-running is EXECUTING: the same permission as triggering from the Studio, never the read one.

    The execution may have been born in the Portal (`pagina_id` filled, triggered by an operator),
    and there what authorizes is the published page. Redoing does not go through any page: it is a
    direct trigger of the pipeline, so it requires the same as `POST /pipelines/{id}/executar`
    requires of the Studio."""
    pipeline_id = (
        await conn.execute(
            text("SELECT pipeline_id FROM execucoes WHERE id = :id"), {"id": execucao_id}
        )
    ).scalar_one_or_none()
    if pipeline_id is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Execution not found.", status=404)
    await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
    await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")
    return pipeline_id


@router.get("/execucoes/{execucao_id}/reexecucao/plano")
async def plano_reexecucao(
    execucao_id: uuid.UUID, desde_no: str | None = None, ator: uuid.UUID = Editor
) -> dict:
    """What a re-execution would do, without doing it. Feeds the screen's confirmation.

    Returns the nodes that would run, the ones that would be reused from the original execution,
    the **external effects** contained in the stretch (named, with the real idempotency) and the
    credential parameters that will have to be supplied again. The effect warning is the reason
    this route exists apart from the POST: a resent e-mail or a repeated `append` write cannot be
    undone, and whoever clicks needs to see the list BEFORE."""
    async with transacao() as conn:
        await _autorizar_reexecucao(conn, ator, execucao_id)
        return await reexecucao.plano(conn, execucao_id, desde_no)


@router.post("/execucoes/{execucao_id}/reexecutar")
async def reexecutar(
    execucao_id: uuid.UUID,
    request: Request,
    dados: ReexecutarIn | None = Body(None),
    ator: uuid.UUID = Editor,
) -> dict:
    """Redo this execution, whole or from a node (`desde_no`).

    Creates a **NEW** execution (`origem='reexecucao'`, `reexecucao_de` pointing at this one):
    reopening the original would erase the evidence that there was a failure. With `desde_no`, the
    upstream does NOT run: its snapshots are copied to the new execution as reused, and only the
    starting node ignores the reuse; descendants recompute on their own if its content changes.

    The executed version is the SAME as the original's (never "the latest"), and so are the
    parameters; from the body only credential ones are accepted, which cannot be reused because
    they are persisted redacted."""
    desde_no = dados.desde_no if dados else None
    parametros = dados.parametros if dados else None
    async with transacao() as conn:
        await _autorizar_reexecucao(conn, ator, execucao_id)

    ctx = await reexecucao.preparar(
        execucao_id=execucao_id,
        ator_id=ator,
        desde_no=desde_no,
        parametros=parametros,
        ip=_ip(request),
        request_id=request_id_atual(),
    )
    from cflow_core.config import get_settings

    cfg = get_settings()
    if cfg.usar_temporal:
        from temporalio.client import Client

        cliente = await Client.connect(cfg.temporal_host)
        args = [
            ctx["execucao_id"],
            ctx["definicao"],
            ctx["ordem"],
            ctx["parametros"],
            ctx["fontes_execucao"],
            ctx["forcar_no"],
        ]
        # Upstream of a chain whose root declares `montante_publica: false`: the re-execution
        # inherits the decision (`reexecucao.preparar`). Positional after `forcar_no`, and only when
        # on; without it the arguments are the usual ones, identical to the history already stored
        # in Temporal.
        if ctx.get("sem_publicar"):
            args.append(True)
        await cliente.start_workflow(
            "ExecutarPipeline",
            args=args,
            id=f"exec-{ctx['execucao_id']}",
            task_queue=cfg.temporal_task_queue,
        )
        return {"id": ctx["execucao_id"], "status": "executando", "versao": ctx["versao"]}

    try:
        return await execucao.rodar_ctx(
            ctx,
            fontes_execucao=ctx["fontes_execucao"],
            forcar_no=ctx["forcar_no"],
            sem_publicar=bool(ctx.get("sem_publicar")),
        )
    except ErroAplicacao as exc:
        # Same rule as /executar: the execution ALREADY exists, and a node that fails is a business
        # outcome. Returning only the envelope would leave whoever re-ran without the id, with no
        # path to the detail.
        exec_id = getattr(exc, "execucao_id", None)
        if exec_id is None:
            raise
        return {"id": exec_id, "status": "falha", "erro": exc.mensagem}


@router.get("/execucoes/{execucao_id}/nos/{no_id}/preview")
async def preview_no_execucao(
    execucao_id: uuid.UUID,
    no_id: str,
    limite: int = Query(default=50, ge=1, le=200),
    ator: uuid.UUID = Editor,
) -> dict:
    """Sample of the node's snapshot + log (status/error/metrics): the Studio drill in Executions.
    `?limite=` (1..200) chooses how many rows come back.

    **Specialist/admin only, and never for a `sistema` pipeline.** A node snapshot is RAW source
    data: it did not go through a named measure nor the page filters. Making it readable by whoever
    triggered the execution would expose, through the Portal, columns the page never exposed (the
    operator who clicks "Generate report" ends up as `iniciada_por`), against the design of a
    read-only Portal governed by the model. Following the execution (status per node) stays open at
    `GET /execucoes/{id}`; seeing the raw data does not."""
    async with transacao() as conn:
        pipeline_id = (
            await conn.execute(
                text("SELECT pipeline_id FROM execucoes WHERE id = :id"), {"id": execucao_id}
            )
        ).scalar_one_or_none()
        if pipeline_id is None:
            raise ErroAplicacao(
                codigo="nao_encontrado", mensagem="Execution not found.", status=404
            )
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await execucao.preview_snapshot(conn, execucao_id, no_id, limite=limite)


class AgendaSpec(BaseModel):
    tipo: str  # hourly | daily | weekly | monthly (values: horaria | diaria | semanal | mensal)
    a_cada_horas: int | None = None  # hourly
    horario: str | None = None  # "HH:MM" (daily/weekly/monthly)
    dias_semana: list[int] | None = None  # weekly: 0=Sunday..6=Saturday
    dia_mes: int | None = None  # monthly: 1..31


class AgendamentoIn(BaseModel):
    agenda: AgendaSpec
    nome: str | None = None
    pagina_id: uuid.UUID | None = None  # filled = PAGE schedule (warms up the day cache)
    parametros: dict | None = None  # parameter values; dates may use a token via "@periodo"
    vigencia_inicio: date | None = None
    vigencia_fim: date | None = None


@router.post("/pipelines/{pipeline_id}/agendamentos", status_code=201)
async def criar_agendamento(
    pipeline_id: uuid.UUID, dados: AgendamentoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pipeline", pipeline_id, "executar")
        return await agendador.criar(
            conn,
            pipeline_id=pipeline_id,
            agenda=dados.agenda.model_dump(exclude_none=True),
            nome=dados.nome,
            pagina_id=dados.pagina_id,
            parametros=dados.parametros,
            vigencia_inicio=dados.vigencia_inicio,
            vigencia_fim=dados.vigencia_fim,
            criado_por=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.get("/pipelines/{pipeline_id}/agendamentos")
async def listar_agendamentos(pipeline_id: uuid.UUID, ator: uuid.UUID = Editor) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, "pipeline", pipeline_id)
        return await agendador.listar(conn, pipeline_id)


@router.get("/agendamentos")
async def listar_agendamentos_todos(ator: uuid.UUID = Editor) -> list[dict]:
    """GLOBAL list of schedules (like the other entities), for the Schedules screen."""
    async with transacao() as conn:
        return await agendador.listar_todos(conn)


@router.patch("/agendamentos/{agendamento_id}", status_code=204)
async def editar_agendamento(
    agendamento_id: uuid.UUID, dados: AgendamentoIn, request: Request, ator: uuid.UUID = Editor
) -> Response:
    async with transacao() as conn:
        await _exigir_no_agendamento(conn, ator, agendamento_id)
        await agendador.editar(
            conn,
            agendamento_id,
            agenda=dados.agenda.model_dump(exclude_none=True),
            nome=dados.nome,
            pagina_id=dados.pagina_id,
            parametros=dados.parametros,
            vigencia_inicio=dados.vigencia_inicio,
            vigencia_fim=dados.vigencia_fim,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.post("/agendamentos/{agendamento_id}/ativar", status_code=204)
async def ativar_agendamento(
    agendamento_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor
) -> Response:
    async with transacao() as conn:
        await _exigir_no_agendamento(conn, ator, agendamento_id)
        await agendador.ativar(
            conn, agendamento_id, ator_id=ator, ip=_ip(request), request_id=request_id_atual()
        )
    return Response(status_code=204)


@router.post("/agendamentos/{agendamento_id}/desativar", status_code=204)
async def desativar_agendamento(
    agendamento_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor
) -> Response:
    async with transacao() as conn:
        await _exigir_no_agendamento(conn, ator, agendamento_id)
        await agendador.desativar(
            conn, agendamento_id, ator_id=ator, ip=_ip(request), request_id=request_id_atual()
        )
    return Response(status_code=204)


@router.post("/agendamentos/{agendamento_id}/executar-agora")
async def executar_agora_agendamento(
    agendamento_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor
) -> dict:
    """Trigger the schedule NOW (test/force), without changing the next run.

    Uses the SAME trigger as the worker's scheduler (`cflow_core.execucao.disparo`): this button
    exists to test the schedule, so it chains the same way. If it ran the pipeline alone here and
    the chain there, the button would validate an operation other than the one that will happen
    overnight.
    """
    from cflow_core.config import get_settings
    from cflow_core.execucao import disparo

    async with transacao() as conn:
        await _exigir_no_agendamento(conn, ator, agendamento_id)
    cfg = get_settings()
    iniciar: disparo.Iniciador | None = None
    if cfg.usar_temporal:
        from temporalio.client import Client

        cliente = await Client.connect(cfg.temporal_host)

        async def _iniciar(nome: str, args: list, workflow_id: str) -> None:
            await cliente.start_workflow(
                nome, args=args, id=workflow_id, task_queue=cfg.temporal_task_queue
            )

        iniciar = _iniciar

    _disp = disparo.montar_dispatch(
        iniciar, ator_id=ator, ip=_ip(request), request_id=request_id_atual()
    )
    return await agendador.executar_agora(
        agendamento_id, _disp, ator_id=ator, ip=_ip(request), request_id=request_id_atual()
    )


async def _exigir_no_agendamento(conn, ator: uuid.UUID, agendamento_id: uuid.UUID) -> None:
    """'executar' permission on the pipeline that OWNS the schedule (404 if it does not exist)."""
    from sqlalchemy import text as _text

    pid = (
        await conn.execute(
            _text("SELECT pipeline_id FROM agendamentos WHERE id = :id"), {"id": agendamento_id}
        )
    ).scalar_one_or_none()
    if pid is None:
        from cflow_core.errors import ErroAplicacao

        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Schedule not found.", status=404)
    await permissoes.exigir(conn, ator, "pipeline", pid, "executar")
