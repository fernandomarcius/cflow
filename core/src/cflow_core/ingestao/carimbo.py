"""OBSOLETE stamp on the scope pointer of a published dataset.

When a dataset changes partitioning (for example `record_date` was added next to `unit_id`), the
pointer of the PREVIOUS layout (`<dataset>/unit_id=11/_atual.json`) stops being moved but stays
readable: a `origem: "publicado"` source that was not migrated along with it would serve the
snapshot from the migration day, forever, without any error. The stamp closes that gap:
`leitura.ler_ponteiro` refuses a stamped body with `lake_escopo_obsoleto`, naming the current
layout.

The operation belongs to the PLATFORM: any partitioned silver/gold dataset, by an admin, through
the API or the UI, and every write goes to the audit trail.

## What decides that a scope belongs to the "previous layout"

The PUBLISHED DEFINITIONS, not the bucket. A scope belongs to a previous layout when no published
pipeline writes the dataset with those partition keys, in that order. `Armazenamento` has no
`listar()`, and the dataset index only changes layout on the first run of the new layout - the
definition says which layout is in use from the moment of publishing. Stamping a scope of the
CURRENT layout would refuse reads of live data with a message saying "partitioning changed", so
it is refused.

## Which scopes belong to the previous layout (`descobrir`)

While the dataset index still describes the previous layout, it lists the scopes; after the first
run of the new layout, it no longer does. The list is still derivable without `listar()`: the
DATABASE says which versions wrote the dataset in the previous layout and which of their runs
finished; the parameter scope comes from the persisted parameters, and the column scope from the
IMMUTABLE `_indice.json` of each run - a path that is computed, not searched.

## Rules
- Only the SCOPE pointer. `<dataset>/_atual.json` is the run index (read-merge-write of the
  publication, key order on read) and is never stamped.
- The previous body stays next to the mark: it says where the pointer used to point, which is what
  anyone investigating old data comes looking for. Unstamping removes only the mark.
- Idempotent: stamping a stamped scope and unstamping a current one write nothing and leave no
  audit entry.
- `simular` returns what would be written, without writing and without an audit entry.
- Under the dataset advisory lock (the same one used to publish the index), with the audit entry
  in the SAME transaction, written BEFORE the object: if the lake write fails, the rollback takes
  the audit entry with it.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from ..audit import Acao, registrar
from ..audit.events import AtorTipo
from ..db import transacao, trava_consultiva
from ..errors import ErroAplicacao
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao
from . import armazenamento, leitura
from .publicados import exigir_camada

RECURSO_TIPO = "lake_escopo"
# The hive segment key (`key=value`): the `chave` declared in the partition item, or the lowercase
# `nome` (`engine._particao_de_parametro`). A plain identifier - nothing that turns into a path.
_CHAVE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_MOTIVO_MAX = 500


def interpretar_particao(itens) -> dict[str, str]:
    """`["unit_id=11", "record_date=2026-08-06"]` -> `{key: value}` IN PATH ORDER.

    A list of `key=value` rather than an object: the hive path is positional, and the key order is
    the address of the pointer itself. The value goes through the SAME check as publishing
    (`leitura.valor_particao`): whatever cannot become a path is rejected, never "fixed"."""
    if not isinstance(itens, list) or not itens:
        raise ErroAplicacao(
            codigo="particao_invalida",
            mensagem=("Provide the scope: the pointer partition, in path order (e.g. unit_id=11)."),
            status=400,
        )
    particao: dict[str, str] = {}
    for bruto in itens:
        if not isinstance(bruto, str) or "=" not in bruto:
            raise ErroAplicacao(
                codigo="particao_invalida",
                mensagem=f"Partition item {bruto!r} is not in key=value format.",
                status=400,
            )
        chave, valor = bruto.split("=", 1)
        chave = chave.strip()
        if not _CHAVE.match(chave):
            raise ErroAplicacao(
                codigo="particao_invalida",
                mensagem=f"Invalid partition key: {chave!r}.",
                status=400,
            )
        if chave in particao:
            raise ErroAplicacao(
                codigo="particao_invalida",
                mensagem=f"The key '{chave}' appears more than once in the scope.",
                status=400,
            )
        particao[chave] = leitura.valor_particao(chave, valor)
    return particao


def _motivo(texto) -> str:
    motivo = texto.strip() if isinstance(texto, str) else ""
    if not motivo:
        raise ErroAplicacao(
            codigo="motivo_obrigatorio",
            mensagem=(
                "Say why the scope changes state: the reason goes to the audit trail and to the "
                "pointer itself, which is where anyone investigating old data will look."
            ),
            status=400,
        )
    if len(motivo) > _MOTIVO_MAX:
        raise ErroAplicacao(
            codigo="motivo_longo",
            mensagem=f"The reason has {len(motivo)} characters; the limit is {_MOTIVO_MAX}.",
            status=400,
        )
    return motivo


@dataclass(frozen=True)
class Produtor:
    """A `saida` node of a PUBLISHED pipeline that writes the dataset to the lake."""

    pipeline_id: str
    nome: str
    conexao_id: str | None
    chaves: tuple[str, ...]


def _chaves_da_saida(cfg: dict) -> tuple[str, ...]:
    """The path keys, in declared order - the engine rule: `chave` or lowercase `nome`."""
    return tuple(
        str(p.get("chave") or str(p.get("nome") or "").lower())
        for p in cfg.get("particao") or []
        if isinstance(p, dict)
    )


def _publica(no: dict, camada: str, dataset: str) -> bool:
    """Does the node write `(dataset, camada)` to the lake? A missing layer defaults to `silver`."""
    cfg = no.get("config") or {}
    return (
        no.get("tipo") == "saida"
        and str(cfg.get("destino") or "arquivo").lower() == "lake"
        and cfg.get("dataset") == dataset
        and str(cfg.get("camada") or "silver") == camada
    )


async def produtores(conn, camada: str, dataset: str) -> list[Produtor]:
    """Who publishes `(dataset, camada)`, read from each pipeline's PUBLISHED definition - a draft
    publishes nothing, and a hand-written list would silently go stale."""
    linhas = (
        (
            await conn.execute(
                text(
                    "SELECT p.id::text AS id, p.nome, v.definicao FROM pipelines p "
                    "JOIN pipeline_versoes v ON v.id = p.versao_publicada_id ORDER BY p.nome"
                )
            )
        )
        .mappings()
        .all()
    )
    achados: list[Produtor] = []
    for r in linhas:
        definicao = r["definicao"]
        if isinstance(definicao, str):
            definicao = json.loads(definicao)
        for no in (definicao or {}).get("nos") or []:
            if _publica(no, camada, dataset):
                cfg = no.get("config") or {}
                achados.append(
                    Produtor(r["id"], r["nome"], cfg.get("conexao_id"), _chaves_da_saida(cfg))
                )
    return achados


def _conexao(prods: list[Produtor], camada: str, dataset: str) -> str | None:
    if not prods:
        raise ErroAplicacao(
            codigo="publicado_sem_produtor",
            mensagem=(
                f"No published pipeline writes dataset '{dataset}' in layer '{camada}'. The "
                "published definition says which lake holds the pointer and which layout is current."
            ),
            status=404,
        )
    if len({p.conexao_id or "" for p in prods}) > 1:
        nomes = ", ".join(f"'{p.nome}'" for p in prods)
        raise ErroAplicacao(
            codigo="publicado_conexao_ambigua",
            mensagem=(
                f"Dataset '{dataset}' (layer '{camada}') is published to more than one lake by "
                f"{nomes}. There is no way to tell which one holds the pointer."
            ),
            status=409,
        )
    return prods[0].conexao_id


def _caminho(dataset: str, particao: dict[str, str], *, ambiente: str | None = None) -> str:
    return f"{leitura.prefixo_escopo(dataset, particao, ambiente=ambiente)}/{leitura.PONTEIRO}"


async def _ler_json(lake, camada: str, caminho: str):
    try:
        return json.loads(await lake.ler(camada, caminho))
    except ErroAplicacao as e:
        if e.status == 404:
            return None
        raise


def _estado(corpo) -> str:
    if corpo is None:
        return "ausente"
    if isinstance(corpo, dict) and corpo.get(leitura.MARCA_OBSOLETO):
        return "obsoleto"
    return "vigente"


def _bloqueio_de_carimbar(
    prods: list[Produtor], particao: dict[str, str], corpo
) -> ErroAplicacao | None:
    """Why THIS scope cannot be stamped, or None. In the order the question is asked."""
    chaves = tuple(particao)
    vigente = next((p for p in prods if p.chaves == chaves), None)
    if vigente is not None:
        return ErroAplicacao(
            codigo="escopo_do_layout_vigente",
            mensagem=(
                f"Layout {'/'.join(chaves)} is still published by '{vigente.nome}'. Stamping this "
                "scope would refuse reads of live data saying it changed partitioning - the "
                "stamp is for scopes of a PREVIOUS layout."
            ),
            status=409,
        )
    layouts = sorted({p.chaves for p in prods})
    if len(layouts) > 1:
        return ErroAplicacao(
            codigo="layout_vigente_ambiguo",
            mensagem=(
                "The published pipelines write this dataset in different layouts ("
                + "; ".join("/".join(c) for c in layouts)
                + "). Without a single current layout, the stamp has nothing to point to."
            ),
            status=409,
        )
    if corpo is None:
        return ErroAplicacao(
            codigo="escopo_ausente",
            mensagem="There is no pointer in this scope: nothing was published with this partition.",
            status=404,
        )
    if not isinstance(corpo, dict) or "caminho" not in corpo:
        return ErroAplicacao(
            codigo="escopo_nao_e_ponteiro",
            mensagem=(
                "The object at this path is not a scope pointer (it is a run index or something "
                "else) and is not stamped."
            ),
            status=409,
        )
    return None


def _aponta(corpo) -> dict | None:
    if not isinstance(corpo, dict):
        return None
    campos = ("execucao_id", "pipeline_id", "momento", "linhas", "caminho")
    return {c: corpo[c] for c in campos if c in corpo}


def _plano(
    camada: str,
    dataset: str,
    particao: dict[str, str],
    prods,
    corpo,
    *,
    ambiente: str | None = None,
) -> dict:
    """The scope state and what can be done with it. It is both the query answer and the `before`
    of the change - the same function, so the UI never shows one thing while the write decides
    another."""
    estado = _estado(corpo)
    bloqueio = _bloqueio_de_carimbar(prods, particao, corpo) if estado != "obsoleto" else None
    layouts = sorted({p.chaves for p in prods})
    return {
        "camada": camada,
        "dataset": dataset,
        "particao": [f"{c}={v}" for c, v in particao.items()],
        "caminho": _caminho(dataset, particao, ambiente=ambiente),
        "estado": estado,
        "layout_vigente": list(layouts[0]) if len(layouts) == 1 else None,
        "produtores": [
            {"pipeline_id": p.pipeline_id, "nome": p.nome, "chaves": list(p.chaves)} for p in prods
        ],
        "aponta": _aponta(corpo),
        "carimbo": corpo.get(leitura.MARCA_OBSOLETO) if isinstance(corpo, dict) else None,
        "pode_carimbar": estado == "vigente" and bloqueio is None,
        "pode_descarimbar": estado == "obsoleto",
        "bloqueio": {"codigo": bloqueio.codigo, "mensagem": bloqueio.mensagem}
        if bloqueio
        else None,
    }


async def consultar(
    *, camada: str, dataset: str, particao_bruta, ambiente: str | None = None
) -> dict:
    """The pointer of ONE scope and what stamping/unstamping would do to it. Writes nothing."""
    exigir_camada(camada)
    ambiente = leitura.ambiente_da_camada(
        camada, ambiente=ambiente, obrigatorio_gold=camada == "gold"
    )
    particao = interpretar_particao(particao_bruta)
    if leitura.CHAVE_AMBIENTE in particao:
        raise ErroAplicacao(
            codigo="particao_invalida",
            mensagem="Environment is context of the Gold root, not a partition key.",
            status=400,
        )
    async with transacao() as conn:
        prods = await produtores(conn, camada, dataset)
    # Outside the transaction: reading the lake (and downloading from the bucket) while holding a
    # pool connection would pin the metadata `xmin` for the duration of the network call - the
    # same rule as reading `/publicados`.
    lake = await armazenamento.abrir(_conexao(prods, camada, dataset))
    corpo = await _ler_json(lake, camada, _caminho(dataset, particao, ambiente=ambiente))
    return _plano(camada, dataset, particao, prods, corpo, ambiente=ambiente)


# --- Discovering the scopes of the PREVIOUS layout ---------------------------------------------

# Runs inspected per discovery. Above this, the answer says `truncado` - never a partial list that
# looks complete.
_TETO_EXECUCOES = 5000
# Simultaneous lake reads (indexes and pointers): each one is a GET against the bucket.
_LEITURAS_EM_PARALELO = 8


@dataclass(frozen=True)
class _SaidaAnterior:
    """A `saida` node of a VERSION - published or not, since the Studio "Run" executes the latest -
    that wrote the dataset in a layout no published pipeline uses anymore."""

    pipeline_id: str
    nome: str
    numero: int
    cfg: dict
    chaves: tuple[str, ...]
    tipos_parametro: dict


async def _saidas_anteriores(
    conn, camada: str, dataset: str, vigentes: set[tuple[str, ...]]
) -> list[_SaidaAnterior]:
    """Every version whose `saida` node writes `(dataset, camada)` partitioned in a layout outside
    the published ones. `@>` only pre-filters by dataset; destination and layer are checked by the
    SAME rule as `produtores` (`destino` may use another case, `camada` may be missing)."""
    from ..pipelines.definicao import parametros_declarados

    filtro = json.dumps({"nos": [{"config": {"dataset": dataset}}]})
    linhas = (
        (
            await conn.execute(
                text(
                    "SELECT v.pipeline_id::text AS pid, p.nome, v.numero, v.definicao "
                    "FROM pipeline_versoes v JOIN pipelines p ON p.id = v.pipeline_id "
                    "WHERE v.definicao @> CAST(:f AS jsonb) ORDER BY p.nome, v.numero"
                ),
                {"f": filtro},
            )
        )
        .mappings()
        .all()
    )
    achados: list[_SaidaAnterior] = []
    for r in linhas:
        definicao = r["definicao"]
        if isinstance(definicao, str):
            definicao = json.loads(definicao)
        # The types of the version that ran: they decide whether `competencia` enters the path
        # through `_inicio` - the same rule publishing used to write the pointer.
        tipos = {
            p["nome"]: p.get("tipo")
            for p in parametros_declarados(definicao or {})
            if isinstance(p, dict) and p.get("nome")
        }
        for no in (definicao or {}).get("nos") or []:
            if not _publica(no, camada, dataset):
                continue
            cfg = no.get("config") or {}
            chaves = _chaves_da_saida(cfg)
            if chaves and chaves not in vigentes:
                achados.append(_SaidaAnterior(r["pid"], r["nome"], r["numero"], cfg, chaves, tipos))
    return achados


def escopos_do_indice(documento, chaves: tuple[str, ...]) -> list[dict[str, str]] | None:
    """The scopes listed by the `_indice.json` of ONE run, in path order. `None` when the document
    is not the index of that layout - counting it as "no scopes" would hide the defect."""
    if not isinstance(documento, dict) or not isinstance(documento.get("particoes"), list):
        return None
    if tuple(documento.get("chaves") or ()) != chaves:
        return None
    fora: list[dict[str, str]] = []
    for p in documento["particoes"]:
        particao = p.get("particao") if isinstance(p, dict) else None
        if not isinstance(particao, dict) or set(particao) != set(chaves):
            return None
        fora.append({c: str(particao[c]) for c in chaves})
    return fora


def _ordem(chaves: tuple[str, ...], particao: dict[str, str]) -> tuple:
    """Display order: by layout and, within it, numbers as numbers (`2` before `11`)."""
    return (
        chaves,
        tuple((0, int(v), "") if v.isdigit() else (1, 0, v) for v in particao.values()),
    )


async def descobrir(*, camada: str, dataset: str, ambiente: str | None = None) -> dict:
    """The scopes of the PREVIOUS layout of the dataset, with the state of each pointer and what the
    stamp would do to it - the same `_plano` as the single-scope query. Writes nothing and leaves
    no audit entry.

    Coverage, stated in the answer: PARAMETER scopes come from the persisted parameters of every
    finished run of the previous version; COLUMN scopes come from the immutable `_indice.json` of
    each run. A COMPLETED run without an index is counted (`sem_indice`), never silently skipped."""
    from ..execucao import engine

    exigir_camada(camada)
    ambiente = leitura.ambiente_da_camada(
        camada, ambiente=ambiente, obrigatorio_gold=camada == "gold"
    )
    async with transacao() as conn:
        prods = await produtores(conn, camada, dataset)
        conexao_atual = _conexao(prods, camada, dataset)
        saidas = await _saidas_anteriores(conn, camada, dataset, {p.chaves for p in prods})
        execucoes: list[tuple[_SaidaAnterior, dict]] = []
        truncado = False
        for s in saidas:
            if truncado:
                break
            linhas = (
                (
                    await conn.execute(
                        text(
                            "SELECT id::text AS id, parametros, status::text AS status "
                            "FROM execucoes WHERE pipeline_id = :p AND versao_numero = :n "
                            "AND status::text <> 'executando' ORDER BY iniciada_em, id LIMIT :t"
                        ),
                        {"p": s.pipeline_id, "n": s.numero, "t": _TETO_EXECUCOES + 1},
                    )
                )
                .mappings()
                .all()
            )
            for r in linhas:
                if len(execucoes) >= _TETO_EXECUCOES:
                    truncado = True
                    break
                parametros = r["parametros"]
                if isinstance(parametros, str):
                    parametros = json.loads(parametros)
                if camada == "gold" and (parametros or {}).get("ambiente") != ambiente:
                    continue
                execucoes.append((s, {**r, "parametros": parametros or {}}))

    # Outside the transaction: lake network calls do not hold a pool connection (same rule as the
    # single-scope query).
    por_layout: dict[tuple[str, ...], dict] = {}
    achados: dict[tuple[tuple[str, ...], tuple], dict[str, str]] = {}
    indices: list[tuple[_SaidaAnterior, dict, str]] = []
    for s, ex in execucoes:
        resumo = por_layout.setdefault(
            s.chaves, {"chaves": list(s.chaves), "versoes": {}, "execucoes": 0, "sem_indice": 0}
        )
        resumo["versoes"].setdefault((s.pipeline_id, s.nome), set()).add(s.numero)
        resumo["execucoes"] += 1
        cfg_particao = list(s.cfg.get("particao") or [])
        try:
            base = engine._particao_de_parametro(
                cfg_particao, ex["parametros"], tipos_parametro=s.tipos_parametro
            )
        except ErroAplicacao:
            continue  # partition parameter without a value: publishing would have been refused
        if all(it.get("origem") == "parametro" for it in cfg_particao):
            achados[(s.chaves, tuple(base.items()))] = base
            continue
        prefixo = engine._prefixo_imutavel(
            dataset,
            cfg_particao,
            base,
            camada=camada,
            ambiente=ex["parametros"].get("ambiente"),
        )
        indices.append((s, ex, f"{prefixo}/execucao_id={ex['id']}/{leitura.INDICE}"))

    lakes: dict[str | None, armazenamento.Armazenamento] = {}
    trava = asyncio.Semaphore(_LEITURAS_EM_PARALELO)

    async def _lake(conexao_id):
        if conexao_id not in lakes:
            lakes[conexao_id] = await armazenamento.abrir(conexao_id)
        return lakes[conexao_id]

    async def _ler_indice(s: _SaidaAnterior, rel: str):
        lake = await _lake(s.cfg.get("conexao_id"))
        async with trava:
            return await _ler_json(lake, camada, rel)

    documentos = await asyncio.gather(*(_ler_indice(s, rel) for s, _ex, rel in indices))
    for (s, ex, _rel), documento in zip(indices, documentos, strict=True):
        escopos = escopos_do_indice(documento, s.chaves)
        if escopos is None:
            # A run that FAILED may never have published; a completed one must have an index.
            if ex["status"] == "concluida":
                por_layout[s.chaves]["sem_indice"] += 1
            continue
        for particao in escopos:
            achados[(s.chaves, tuple(particao.items()))] = particao

    # The pointer of each scope lives in the lake of the CURRENT producers - the same one the stamp
    # writes to.
    lake_atual = await _lake(conexao_atual)

    async def _plano_do(particao: dict[str, str]) -> dict:
        async with trava:
            corpo = await _ler_json(
                lake_atual, camada, _caminho(dataset, particao, ambiente=ambiente)
            )
        return _plano(camada, dataset, particao, prods, corpo, ambiente=ambiente)

    ordenados = sorted(achados.items(), key=lambda kv: _ordem(kv[0][0], kv[1]))
    planos = await asyncio.gather(*(_plano_do(p) for _k, p in ordenados))
    layouts = sorted({p.chaves for p in prods})
    return {
        "camada": camada,
        "dataset": dataset,
        "layout_vigente": list(layouts[0]) if len(layouts) == 1 else None,
        "produtores": [
            {"pipeline_id": p.pipeline_id, "nome": p.nome, "chaves": list(p.chaves)} for p in prods
        ],
        "layouts_anteriores": [
            {
                "chaves": r["chaves"],
                "versoes": [
                    {"pipeline_id": pid, "nome": nome, "numeros": sorted(numeros)}
                    for (pid, nome), numeros in sorted(
                        r["versoes"].items(), key=lambda kv: kv[0][1]
                    )
                ],
                "execucoes": r["execucoes"],
                "sem_indice": r["sem_indice"],
            }
            for r in sorted(por_layout.values(), key=lambda r: r["chaves"])
        ],
        # Only scopes WITH a pointer: a missing one has nothing to stamp (the run never published,
        # or published to another lake) - it enters the count, not the list.
        "escopos": [p for p in planos if p["estado"] != "ausente"],
        "ausentes": sum(1 for p in planos if p["estado"] == "ausente"),
        "truncado": truncado,
    }


def _diff(plano: dict, marca) -> dict:
    return {
        "camada": plano["camada"],
        "dataset": plano["dataset"],
        "caminho": plano["caminho"],
        "particao": plano["particao"],
        "aponta_execucao": (plano["aponta"] or {}).get("execucao_id"),
        "obsoleto": marca,
    }


async def _mudar(
    *,
    carimbar: bool,
    camada: str,
    dataset: str,
    particao_bruta,
    ambiente: str | None = None,
    motivo,
    ator_id: uuid.UUID | None,
    ip: str | None = None,
    request_id: str | None = None,
    simular: bool = False,
) -> dict:
    exigir_camada(camada)
    ambiente = leitura.ambiente_da_camada(
        camada, ambiente=ambiente, obrigatorio_gold=camada == "gold"
    )
    particao = interpretar_particao(particao_bruta)
    if leitura.CHAVE_AMBIENTE in particao:
        raise ErroAplicacao(
            codigo="particao_invalida",
            mensagem="Environment is context of the Gold root, not a partition key.",
            status=400,
        )
    motivo = _motivo(motivo)
    if not simular and ator_id is None:
        raise ValueError(
            "stamp without an actor: the audit trail must say WHO changed the pointer."
        )
    caminho = _caminho(dataset, particao, ambiente=ambiente)
    # The SAME lock as index publishing (`leitura.publicar_indice`): two admins on the same dataset,
    # or an admin and a publication, do not cross paths in the pointer read-modify-write.
    raiz_dataset = leitura.prefixo_dataset(dataset, ambiente)
    async with trava_consultiva(f"cflow:lake:{camada}:{raiz_dataset}") as conn:
        prods = await produtores(conn, camada, dataset)
        lake = await armazenamento.abrir(_conexao(prods, camada, dataset))
        corpo = await _ler_json(lake, camada, caminho)
        plano = _plano(camada, dataset, particao, prods, corpo, ambiente=ambiente)
        if carimbar:
            if plano["estado"] == "obsoleto":
                return {**plano, "mudou": False, "simulado": simular}
            erro = _bloqueio_de_carimbar(prods, particao, corpo)
            if erro is not None:
                raise erro
            antes = None
            marca = {
                "chaves": plano["layout_vigente"],
                "desde": para_exibicao(agora_utc(), FUSO_SAO_PAULO).isoformat(),
                "motivo": motivo,
            }
            novo = {**corpo, leitura.MARCA_OBSOLETO: marca}
        else:
            if plano["estado"] == "vigente":
                return {**plano, "mudou": False, "simulado": simular}
            if plano["estado"] == "ausente":
                raise ErroAplicacao(
                    codigo="escopo_ausente",
                    mensagem=(
                        "There is no pointer in this scope: nothing was published with this "
                        "partition."
                    ),
                    status=404,
                )
            antes = corpo[leitura.MARCA_OBSOLETO]
            marca = None
            novo = {k: v for k, v in corpo.items() if k != leitura.MARCA_OBSOLETO}
        depois = _plano(camada, dataset, particao, prods, novo, ambiente=ambiente)
        if simular:
            return {**depois, "mudou": True, "simulado": True}
        # Audit entry BEFORE the object, in the lock transaction: if the lake write fails, the
        # rollback takes the audit entry with it. The inverse - a changed pointer with no record -
        # is what an out-of-band script would do.
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo=RECURSO_TIPO,
            ator_id=ator_id,
            ator_tipo=AtorTipo.USUARIO,
            antes=_diff(plano, antes),
            depois={
                **_diff(depois, marca),
                "operacao": "carimbar" if carimbar else "descarimbar",
                "motivo": motivo,
            },
            request_id=request_id,
            ip=ip,
        )
        await lake.gravar(
            camada,
            caminho,
            json.dumps(novo, ensure_ascii=False, indent=2).encode(),
            tipo_mime="application/json",
            sobrescrever=True,
        )
    return {**depois, "mudou": True, "simulado": False}


async def carimbar(**kw) -> dict:
    """Stamp the pointer of a previous-layout scope as OBSOLETE. See the module header."""
    return await _mudar(carimbar=True, **kw)


async def descarimbar(**kw) -> dict:
    """Remove the OBSOLETE mark: the pointer is read again as it was before the stamp."""
    return await _mudar(carimbar=False, **kw)
