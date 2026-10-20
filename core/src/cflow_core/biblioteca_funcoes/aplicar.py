"""Load the libraries' MACROs into a DuckDB session: the SAME DDL in preview and in execution
(the same SQL runs on both paths, only the data changes).

- `aplicar`: compiles the definitions into DDL and runs it in DuckDB.
- `carregar_publicadas`: PREVIEW, uses the current PUBLISHED version of each library
  (idempotent; reapplying on every preview picks up newly published versions and avoids a
  stale MACRO).
- `carregar_para_execucao`: EXECUTION, uses the EXACT version pinned in
  `execucoes.bibliotecas_versoes`, guaranteeing reproducibility (not "the latest published").
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ErroAplicacao
from . import service
from .compilar import montar_ddl


def aplicar(duck, definicoes: list[dict]) -> None:
    """Compile `definicoes` (a list of library defs) into MACRO DDL and run it on `duck`.
    No-op if there is no DDL. DuckDB runs several `;`-separated statements in a single
    `execute`; if that fails, it runs statement by statement (robust)."""
    ddl = montar_ddl(definicoes)
    if not ddl:
        return
    try:
        duck.execute(ddl)
    except Exception:  # noqa: BLE001 - fallback: some drivers run only 1 statement at a time
        for stmt in (s.strip() for s in ddl.split(";")):
            if stmt:
                duck.execute(stmt)


async def carregar_publicadas(duck, conn: AsyncConnection, ids: list) -> None:
    """PREVIEW: load the current PUBLISHED version of each library in `ids` (uuid or str).

    A DECLARED library that fails to load is an ERROR (400), never silence: the node would fail
    later with "function does not exist", a message that does not say what to do. And the two
    reasons for not loading call for OPPOSITE actions, so the message tells them apart:

    - **exists here, with no published version** -> it needs publishing (preview only loads
      what is published);
    - **does not exist in this database** -> the reference must be redone. Library ids do not
      cross environments: the same library has a different id in each database.
    """
    pedidos = [i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in (ids or [])]
    if not pedidos:
        return
    defs = await service.obter_publicada(conn, pedidos)
    faltando = [p for p in pedidos if str(p) not in {d["id"] for d in defs}]
    if faltando:
        linhas = (
            await conn.execute(
                text("SELECT id, nome FROM biblioteca_funcoes WHERE id = ANY(:ids)"),
                {"ids": faltando},
            )
        ).all()
        conhecidas = {r.id: r.nome for r in linhas}
        sem_publicacao = [conhecidas[p] for p in faltando if p in conhecidas]
        inexistentes = [str(p) for p in faltando if p not in conhecidas]
        partes: list[str] = []
        if sem_publicacao:
            partes.append(
                "Function library has no published version: "
                + ", ".join(sem_publicacao)
                + ". Publish the library so its functions apply in preview and in execution."
            )
        if inexistentes:
            partes.append(
                "Function library does not exist in this database: "
                + ", ".join(inexistentes)
                + ". Redo the reference with the id generated here; "
                "library ids do not cross environments."
            )
        raise ErroAplicacao(
            codigo=(
                "biblioteca_inexistente"
                if inexistentes and not sem_publicacao
                else "biblioteca_sem_versao_publicada"
            ),
            mensagem=" ".join(partes),
            status=400,
        )
    aplicar(duck, [d["definicao"] for d in defs])


def aplicar_ddl(duck, ddl: str) -> None:
    """Run an already compiled MACRO DDL (see `aplicar`)."""
    if not ddl:
        return
    try:
        duck.execute(ddl)
    except Exception:  # noqa: BLE001 - fallback: some drivers run only 1 statement at a time
        for stmt in (s.strip() for s in ddl.split(";")):
            if stmt:
                duck.execute(stmt)


#: DDL + MACRO names per set of pins. A pinned version (library + number) is IMMUTABLE, so what
#: is read from it holds for the whole process. Without this cache, every node of every
#: execution re-read the definitions from the database and recreated the ~50 MACROs in DuckDB,
#: on the event loop and with the transaction open. The cap only exists so the dictionary does
#: not grow unbounded in a long-lived worker.
_DDL_POR_PIN: dict[tuple, tuple[str, frozenset[str]]] = {}
_TETO_CACHE = 64


async def ddl_para_execucao(
    conn: AsyncConnection, bibliotecas_versoes: list[dict]
) -> tuple[str, frozenset[str]]:
    """EXECUTION: `(ddl, names)` of the MACROs of the EXACT pinned version of each library.
    `bibliotecas_versoes` is the [{"id","numero"}] list from `execucoes.bibliotecas_versoes`. A
    reproducibility pin, not "the latest published". `names` are lower-case (DuckDB identifiers
    are case-insensitive)."""
    if not bibliotecas_versoes:
        return "", frozenset()
    chave = tuple((str(p["id"]), int(p["numero"])) for p in bibliotecas_versoes)
    feito = _DDL_POR_PIN.get(chave)
    if feito is not None:
        return feito
    defs: list[dict] = []
    for bid, numero in chave:
        definicao = (
            await conn.execute(
                text(
                    "SELECT definicao FROM biblioteca_funcoes_versoes "
                    "WHERE biblioteca_id = :b AND numero = :n"
                ),
                {"b": uuid.UUID(bid), "n": numero},
            )
        ).scalar_one_or_none()
        if definicao is not None:
            defs.append(definicao)
    feito = (
        montar_ddl(defs),
        frozenset(str(f["nome"]).lower() for d in defs for f in d.get("funcoes", [])),
    )
    if len(_DDL_POR_PIN) >= _TETO_CACHE:
        _DDL_POR_PIN.clear()
    _DDL_POR_PIN[chave] = feito
    return feito


def ddl_em_cache(bibliotecas_versoes: list[dict]) -> tuple[str, frozenset[str]] | None:
    """The `(ddl, names)` of `ddl_para_execucao` IF already cached, for callers who want to avoid
    opening a transaction just to find out it was not needed. None = go to the database."""
    if not bibliotecas_versoes:
        return "", frozenset()
    return _DDL_POR_PIN.get(tuple((str(p["id"]), int(p["numero"])) for p in bibliotecas_versoes))


def cita_macro(texto: str, nomes: frozenset[str]) -> bool:
    """Does the node text mention any of the MACROs? By substring, no parsing: erring on the side
    of loading costs the usual DDL; erring the other way would break the node. When it mentions
    ONE, ALL are loaded (a MACRO may call another)."""
    if not nomes:
        return False
    t = texto.lower()
    return any(n in t for n in nomes)


async def carregar_para_execucao(
    duck, conn: AsyncConnection, bibliotecas_versoes: list[dict]
) -> None:
    """EXECUTION: load the EXACT pinned version of each library, unconditionally."""
    ddl, _ = await ddl_para_execucao(conn, bibliotecas_versoes)
    aplicar_ddl(duck, ddl)
