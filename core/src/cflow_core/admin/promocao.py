"""Promotion between environments (v2 bundle): export what was PUBLISHED and import it re-pointing
references - from the UI, no script.

The v1 bundle carried the latest version of EVERY pipeline (drafts included), lost `camada`,
`grupo` and `sistema`, re-pointed only the node `conexao_id` - not the `pipeline_id` of a
pipeline-type source, which is precisely the link between pipelines - and created in file order.
The rules below make promotion a platform feature:

- **Only the published version travels**: a draft is work in progress of whoever is editing.
- **Matches by NAME at the destination**, connections and pipelines - ids belong to the
  environment. An existing connection is reused (creating a second one would split the credential
  vault from the first); a new connection is born WITHOUT credential (the vault is per
  environment) and is reported as a pending item.
- **Dependency order:** whatever is read by a `pipeline` source enters first, and the reference is
  rewritten to the destination id. A reference to a pipeline outside the bundle is refused -
  importing half a chain would leave a consumer reading from an id that does not exist here.
- **No overwrite without asking:** an existing pipeline with a different definition only gets a new
  version with `atualizar`; if identical it is left alone - importing twice creates nothing.
- **Dry run writes nothing** and returns the SAME plan that the import executes.
- Whatever belongs to the environment and cannot be re-pointed (ingestion source, function library,
  on-disk output path, version pinned from another pipeline) is reported as a named PENDING item,
  never silently.
"""

from __future__ import annotations

import copy
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import DetalheErro, ErroAplicacao
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

VERSAO_PACOTE = 2
_CLASSIFICACAO = ("camada", "grupo", "sistema")


def _recusa(codigo: str, mensagem: str, detalhes: list[str]) -> ErroAplicacao:
    return ErroAplicacao(
        codigo=codigo,
        mensagem=mensagem,
        status=422,
        detalhes=[DetalheErro(campo="pacote", mensagem=m) for m in detalhes],
    )


def _canonico(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _dependencias(definicao: dict) -> list[str]:
    from ..pipelines.dag import dependencias_diretas

    return [d["pipeline_id"] for d in dependencias_diretas(definicao or {})]


def conexoes_da_definicao(definicao: dict) -> set[str]:
    """Connection ids referenced by the definition: node `conexao_id` (source, write, http, e-mail,
    lake output) and parameters (`opcoes_fonte`, source `derivacao`). A `arquivo` derivation points
    to the connection by NAME (`mapa`) and already travels correctly."""
    ids: set[str] = set()
    for no in (definicao or {}).get("nos") or []:
        cid = (no.get("config") or {}).get("conexao_id") if isinstance(no, dict) else None
        if cid:
            ids.add(str(cid))
    for p in (definicao or {}).get("parametros") or []:
        for bloco in ("opcoes_fonte", "derivacao"):
            b = p.get(bloco) if isinstance(p, dict) else None
            if isinstance(b, dict) and b.get("conexao_id"):
                ids.add(str(b["conexao_id"]))
    return ids


def reapontar(definicao: dict, conexoes: dict[str, str], pipelines: dict[str, str]) -> dict:
    """Copy of the definition with source-environment ids replaced by destination ones. An id that is
    not in the maps is left as it came (the caller decides whether that is a refusal or a pending item)."""
    d = copy.deepcopy(definicao or {})
    for no in d.get("nos") or []:
        cfg = no.get("config") if isinstance(no, dict) else None
        if not isinstance(cfg, dict):
            continue
        cid = cfg.get("conexao_id")
        if cid and str(cid) in conexoes:
            cfg["conexao_id"] = conexoes[str(cid)]
        if no.get("tipo") == "fonte" and (cfg.get("origem") or "conexao") == "pipeline":
            pid = cfg.get("pipeline_id")
            if pid and str(pid) in pipelines:
                cfg["pipeline_id"] = pipelines[str(pid)]
                # The pinned version is a number from the OTHER environment: here the published one applies
                # (and the change is reported as pending in `pendencias_da_definicao`).
                cfg.pop("versao", None)
    for p in d.get("parametros") or []:
        for bloco in ("opcoes_fonte", "derivacao"):
            b = p.get(bloco) if isinstance(p, dict) else None
            if isinstance(b, dict) and b.get("conexao_id") and str(b["conexao_id"]) in conexoes:
                b["conexao_id"] = conexoes[str(b["conexao_id"])]
    return d


def pendencias_da_definicao(nome: str, definicao: dict) -> list[str]:
    """What belongs to the environment and cannot be re-pointed by name - reported, never swallowed."""
    out: list[str] = []
    bibs = list((definicao or {}).get("bibliotecas") or [])
    for no in (definicao or {}).get("nos") or []:
        if not isinstance(no, dict):
            continue
        cfg = no.get("config") or {}
        nid = no.get("id")
        bibs += list(cfg.get("bibliotecas") or [])
        if no.get("tipo") == "ingestao" and cfg.get("fonte_id"):
            out.append(
                f"'{nome}', node '{nid}': reads ingestion source {cfg['fonte_id']} from the source "
                "environment - check the source id in this environment."
            )
        destino = str(cfg.get("destino") or "arquivo").lower()
        if no.get("tipo") == "saida" and destino == "arquivo" and cfg.get("caminho"):
            out.append(
                f"'{nome}', node '{nid}': writes to '{cfg['caminho']}', a path of the source environment."
            )
        if (
            no.get("tipo") == "fonte"
            and (cfg.get("origem") or "conexao") == "pipeline"
            and cfg.get("versao") is not None
        ):
            out.append(
                f"'{nome}', node '{nid}': pinned version {cfg['versao']} of the pipeline it reads, a "
                "number from the source environment - here it reads the published version."
            )
    if bibs:
        out.append(
            f"'{nome}': uses function libraries {sorted({str(b) for b in bibs})} - ids from the "
            "source environment; publish the same libraries here."
        )
    return out


def ordenar(pipelines: list[dict]) -> list[dict]:
    """Dependencies first (`pipeline` source INSIDE the bundle). A cycle is a named refusal. A v1
    bundle (no `id`) keeps file order - there is no way to know who reads whom."""
    if not all(p.get("id") for p in pipelines):
        return list(pipelines)
    por_id = {str(p["id"]): p for p in pipelines}
    deps = {
        pid: [d for d in _dependencias(p["definicao"]) if d in por_id] for pid, p in por_id.items()
    }
    ordem: list[dict] = []
    estado: dict[str, str] = {}

    def visitar(pid: str, trilha: list[str]) -> None:
        if estado.get(pid) == "feito":
            return
        if estado.get(pid) == "andando":
            ciclo = [por_id[x]["nome"] for x in trilha[trilha.index(pid) :]] + [por_id[pid]["nome"]]
            raise _recusa(
                "pacote_com_ciclo", "The bundle has a cycle between pipelines.", [" → ".join(ciclo)]
            )
        estado[pid] = "andando"
        for d in deps[pid]:
            visitar(d, [*trilha, pid])
        estado[pid] = "feito"
        ordem.append(por_id[pid])

    for pid in por_id:
        visitar(pid, [])
    return ordem


def _validar_pacote(pacote: dict) -> None:
    if not isinstance(pacote, dict) or pacote.get("versao") not in (1, VERSAO_PACOTE):
        raise _recusa(
            "pacote_invalido",
            "Invalid promotion bundle.",
            ["Expected the JSON generated by 'Export' (versao 1 or 2)."],
        )
    problemas = []
    for chave in ("conexoes", "pipelines"):
        if not isinstance(pacote.get(chave, []), list):
            problemas.append(f"'{chave}' must be a list.")
    for p in pacote.get("pipelines") or []:
        if not isinstance(p, dict) or not p.get("nome") or not isinstance(p.get("definicao"), dict):
            problemas.append("Every pipeline in the bundle needs 'nome' and 'definicao'.")
            break
    for c in pacote.get("conexoes") or []:
        if not isinstance(c, dict) or not c.get("nome") or not c.get("tipo"):
            problemas.append("Every connection in the bundle needs 'nome' and 'tipo'.")
            break
    if problemas:
        raise _recusa("pacote_invalido", "Invalid promotion bundle.", problemas)


# --- export --------------------------------------------------------------------------------------


async def exportar(conn: AsyncConnection, pipeline_ids: list[uuid.UUID] | None = None) -> dict:
    """The v2 bundle: pipelines in the PUBLISHED version, in dependency order, with classification,
    and connections WITHOUT secrets. With `pipeline_ids`, only their closure (what they read through
    `pipeline` sources, transitively) and the connections that closure uses; without, everything."""
    from ..pipelines import service as ps

    linhas = (
        (
            await conn.execute(
                text(
                    "SELECT id, nome, descricao, camada, grupo, sistema FROM pipelines "
                    "ORDER BY criado_em"
                )
            )
        )
        .mappings()
        .all()
    )
    por_id = {str(r["id"]): r for r in linhas}
    publicadas: dict[str, tuple[int, dict]] = {}

    async def publicada(pid: str) -> tuple[int, dict] | None:
        if pid not in publicadas:
            n = await ps.numero_publicado(conn, uuid.UUID(pid))
            publicadas[pid] = (
                (n, (await ps.obter(conn, uuid.UUID(pid), n))["definicao"]) if n else (0, {})
            )
        return publicadas[pid] if publicadas[pid][0] else None

    if pipeline_ids:
        pedidos = [str(x) for x in pipeline_ids]
        faltando = [x for x in pedidos if x not in por_id]
        if faltando:
            raise ErroAplicacao(
                codigo="nao_encontrado",
                mensagem="Pipeline not found.",
                status=404,
                detalhes=[DetalheErro(campo="pipeline", mensagem=x) for x in faltando],
            )
        alvo: list[str] = []
        fila = list(pedidos)
        while fila:
            pid = fila.pop(0)
            if pid in alvo or pid not in por_id:
                continue
            alvo.append(pid)
            pub = await publicada(pid)
            if pub:
                fila += _dependencias(pub[1])
    else:
        alvo = list(por_id)

    pipelines, sem_publicada = [], []
    for pid in alvo:
        pub = await publicada(pid)
        r = por_id[pid]
        if pub is None:
            sem_publicada.append(r["nome"])
            continue
        pipelines.append(
            {
                "id": pid,
                "nome": r["nome"],
                "descricao": r["descricao"],
                "camada": r["camada"],
                "grupo": r["grupo"],
                "sistema": bool(r["sistema"]),
                "versao": pub[0],
                "definicao": pub[1],
            }
        )

    usadas = (
        set().union(*(conexoes_da_definicao(p["definicao"]) for p in pipelines))
        if pipelines
        else set()
    )
    cxs = (
        (
            await conn.execute(
                text(
                    "SELECT id, nome, tipo::text AS tipo, config, credencial_ref IS NOT NULL AS tem "
                    "FROM conexoes ORDER BY criado_em"
                )
            )
        )
        .mappings()
        .all()
    )
    conexoes = [
        {
            "id": str(c["id"]),
            "nome": c["nome"],
            "tipo": c["tipo"],
            "config": c["config"],
            "tem_credencial": bool(c["tem"]),  # only the flag: the secret does not travel
        }
        for c in cxs
        if not pipeline_ids or str(c["id"]) in usadas
    ]
    return {
        "versao": VERSAO_PACOTE,
        "exportado_em": para_exibicao(agora_utc(), FUSO_SAO_PAULO).isoformat(),
        "conexoes": conexoes,
        "pipelines": ordenar(pipelines),
        "sem_versao_publicada": sem_publicada,
    }


# --- plan / import -------------------------------------------------------------------------------


async def planejar(conn: AsyncConnection, pacote: dict, *, atualizar: bool = False) -> dict:
    """The import plan, writing nothing - the same one `importar` executes."""
    from ..pipelines import service as ps

    _validar_pacote(pacote)
    v2 = pacote.get("versao") == VERSAO_PACOTE
    cx_destino = {
        r["nome"]: str(r["id"])
        for r in (await conn.execute(text("SELECT id, nome FROM conexoes"))).mappings().all()
    }
    pl_destino = {
        r["nome"]: r
        for r in (
            await conn.execute(text("SELECT id, nome, camada, grupo, sistema FROM pipelines"))
        )
        .mappings()
        .all()
    }

    conexoes, mapa_cx, pendencias = [], {}, []
    for c in pacote.get("conexoes") or []:
        existente = cx_destino.get(c["nome"])
        if existente:
            mapa_cx[str(c.get("id"))] = existente
        elif c.get("tem_credencial", True):
            pendencias.append(
                f"Connection '{c['nome']}' is created WITHOUT credential: register it in this environment (the "
                "vault does not travel)."
            )
        conexoes.append(
            {"nome": c["nome"], "tipo": c["tipo"], "acao": "reusar" if existente else "criar"}
        )

    ordem = ordenar(pacote.get("pipelines") or [])
    ids_pacote = {str(p["id"]) for p in ordem if p.get("id")}
    quebradas = []
    if v2:
        for p in ordem:
            for d in _dependencias(p["definicao"]):
                if d not in ids_pacote:
                    quebradas.append(
                        f"'{p['nome']}' reads pipeline {d}, which is not in the bundle - export the closure."
                    )
    if quebradas:
        raise _recusa(
            "pacote_referencia_quebrada",
            "The bundle references a pipeline it does not include.",
            quebradas,
        )

    mapa_pl: dict[str, str] = {}
    itens = []
    for p in ordem:
        existente = pl_destino.get(p["nome"])
        if existente:
            mapa_pl[str(p.get("id"))] = str(existente["id"])
        definicao = reapontar(p["definicao"], mapa_cx, mapa_pl)
        diferencas: list[str] = []
        if existente:
            n = await ps.numero_publicado(conn, existente["id"])
            atual = (await ps.obter(conn, existente["id"], n))["definicao"] if n else None
            if atual is None or _canonico(atual) != _canonico(definicao):
                diferencas.append("definicao")
            if v2:
                diferencas += [
                    k
                    for k in _CLASSIFICACAO
                    if (bool(existente[k]) if k == "sistema" else existente[k])
                    != (bool(p.get(k)) if k == "sistema" else p.get(k))
                ]
            acao = (
                "manter" if not diferencas else ("atualizar" if atualizar else "manter_diferente")
            )
        else:
            acao = "criar"
        pendencias += (
            pendencias_da_definicao(p["nome"], p["definicao"])
            if acao
            in (
                "criar",
                "atualizar",
            )
            else []
        )
        itens.append(
            {
                "nome": p["nome"],
                "acao": acao,
                "diferencas": diferencas,
                **({k: p.get(k) for k in _CLASSIFICACAO} if v2 else {}),
                "versao_origem": p.get("versao"),
            }
        )
    return {
        "versao_pacote": pacote.get("versao"),
        "atualizar": atualizar,
        "conexoes": conexoes,
        "pipelines": itens,
        "pendencias": pendencias,
    }


async def importar(
    conn: AsyncConnection,
    pacote: dict,
    *,
    atualizar: bool = False,
    simular: bool = False,
    ator_id,
    ip=None,
    request_id=None,
) -> dict:
    """Execute the plan in the caller's transaction - all or nothing, each write audited by the
    service that owns it (create/publish/classify). `simular` returns only the plan."""
    from ..conexoes import service as cs
    from ..modelagem import versionado
    from ..pipelines import service as ps

    plano = await planejar(conn, pacote, atualizar=atualizar)
    contagens = {"conexoes_criadas": 0, "pipelines_criados": 0, "pipelines_atualizados": 0}
    if simular:
        return {**plano, **contagens, "simulado": True}

    v2 = pacote.get("versao") == VERSAO_PACOTE
    cx_destino = {
        r["nome"]: str(r["id"])
        for r in (await conn.execute(text("SELECT id, nome FROM conexoes"))).mappings().all()
    }
    mapa_cx: dict[str, str] = {}
    for c in pacote.get("conexoes") or []:
        if c["nome"] in cx_destino:
            mapa_cx[str(c.get("id"))] = cx_destino[c["nome"]]
            continue
        novo = await cs.criar(
            conn,
            nome=c["nome"],
            tipo=c["tipo"],
            config=c.get("config") or {},
            segredo=None,
            ator_id=ator_id,
            ip=ip,
            request_id=request_id,
        )
        mapa_cx[str(c.get("id"))] = str(novo)
        contagens["conexoes_criadas"] += 1

    pl_destino = {
        r["nome"]: str(r["id"])
        for r in (await conn.execute(text("SELECT id, nome FROM pipelines"))).mappings().all()
    }
    acao_de = {i["nome"]: i["acao"] for i in plano["pipelines"]}
    nota = f"Promoted (bundle from {pacote.get('exportado_em') or 'undated source'})."
    mapa_pl: dict[str, str] = {}
    for p in ordenar(pacote.get("pipelines") or []):
        acao = acao_de[p["nome"]]
        if acao in ("manter", "manter_diferente"):
            mapa_pl[str(p.get("id"))] = pl_destino[p["nome"]]
            continue
        definicao = reapontar(p["definicao"], mapa_cx, mapa_pl)
        if acao == "criar":
            pid = await ps.criar(
                conn,
                nome=p["nome"],
                descricao=p.get("descricao") or "imported",
                dono_id=ator_id,
                ip=ip,
                request_id=request_id,
            )
            base = 0
            contagens["pipelines_criados"] += 1
        else:
            pid = uuid.UUID(pl_destino[p["nome"]])
            base = (await ps.obter(conn, pid))["ultimo"]
            contagens["pipelines_atualizados"] += 1
        rascunho = await ps.salvar_rascunho(
            conn,
            pipeline_id=pid,
            definicao=definicao,
            base_numero=base,
            autor_id=ator_id,
            ip=ip,
            request_id=request_id,
        )
        await ps.publicar(
            conn,
            pipeline_id=pid,
            base_numero=rascunho,
            nota=nota,
            autor_id=ator_id,
            ip=ip,
            request_id=request_id,
        )
        if v2:
            await ps.definir_camada(
                conn,
                pipeline_id=pid,
                camada=p.get("camada"),
                ator_id=ator_id,
                ip=ip,
                request_id=request_id,
            )
            await ps.definir_grupo(
                conn,
                pipeline_id=pid,
                grupo=p.get("grupo"),
                ator_id=ator_id,
                ip=ip,
                request_id=request_id,
            )
            await versionado.definir_sistema(
                conn,
                tabela="pipelines",
                recurso_tipo="pipeline",
                recurso_id=pid,
                sistema=bool(p.get("sistema")),
                ator_id=ator_id,
                ip=ip,
                request_id=request_id,
            )
        mapa_pl[str(p.get("id"))] = str(pid)
    return {**plano, **contagens, "simulado": False}
