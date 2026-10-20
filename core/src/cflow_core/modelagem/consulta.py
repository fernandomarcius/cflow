"""Cube/view query: aggregates the source pipeline's execution snapshot in DuckDB.

Every number comes from a NAMED MEASURE of the cube: requested dimensions/measures are resolved
by NAME against the published definition; no free-form expression from the client. Filters
apply to dimension columns (parameterized).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import DetalheErro, ErroAplicacao
from ..execucao.snapshots import resolver_snapshot
from . import versionado

_OPS = {
    "igual": "=",
    "diferente": "!=",
    "maior": ">",
    "menor": "<",
    "maior_igual": ">=",
    "menor_igual": "<=",
    "contem": "LIKE",
}


async def _snapshot_fonte(
    conn: AsyncConnection, fonte: dict, execucao_id: uuid.UUID | str | None = None
) -> dict:
    """Source snapshot + origin (for the stamp's trace chain).

    Delegates to `execucao.snapshots.resolver_snapshot` (same resolution used by the `pipeline`
    source type): with `execucao_id`, reads the snapshot of THAT execution, the one the operator
    triggered, avoiding "last run wins"; without it, falls back to the latest completed one.

    The cube may reference ANOTHER pipeline than the one the page executed (e.g. a page that
    runs the root of a DAG with blocks reading an upstream). In that case the execution of the
    SAME trigger is found through `cadeia_id` (/executar with dependencies runs the whole chain
    under a single id), preserving "all blocks from the same instant". With no sibling in the
    chain (old execution, dependency not declared as an edge), it falls back to the latest
    completed one of the cube's pipeline, and `origem.resolucao` SAYS so (`"ultima"`), so the
    screen and the stamped report do not attribute the data to a trigger that did not produce it.
    """
    resolucao = "ultima" if execucao_id is None else "execucao"
    if execucao_id is not None:
        exec_row = (
            (
                await conn.execute(
                    text("SELECT pipeline_id, cadeia_id FROM execucoes WHERE id = :e"),
                    {"e": uuid.UUID(str(execucao_id))},
                )
            )
            .mappings()
            .first()
        )
        if exec_row and str(exec_row["pipeline_id"]) != str(fonte["pipeline_id"]):
            irmao = None
            if exec_row["cadeia_id"] is not None:
                irmao = (
                    (
                        await conn.execute(
                            text(
                                "SELECT id FROM execucoes WHERE pipeline_id = :p "
                                "AND cadeia_id = :c AND status = 'concluida' "
                                "ORDER BY concluida_em DESC LIMIT 1"
                            ),
                            {
                                "p": uuid.UUID(str(fonte["pipeline_id"])),
                                "c": exec_row["cadeia_id"],
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
            if irmao:
                execucao_id, resolucao = irmao["id"], "cadeia"
            else:
                execucao_id, resolucao = None, "ultima"
    origem = await resolver_snapshot(
        conn, fonte["pipeline_id"], fonte["no_id"], execucao_id=execucao_id
    )
    origem["resolucao"] = resolucao
    return origem


def _where(filtros: list[dict] | None, dim_map: dict) -> tuple[str, list]:
    clauses, params = [], []
    for f in filtros or []:
        col = dim_map.get(f.get("dimensao"))
        if not col:
            raise ErroAplicacao(
                codigo="dimensao_desconhecida",
                mensagem=f"Filter on unknown dimension: {f.get('dimensao')}.",
                status=400,
            )
        op = _OPS.get(f.get("op", "igual"))
        if not op:
            raise ErroAplicacao(
                codigo="op_invalido", mensagem="Invalid filter operator.", status=400
            )
        val = f.get("valor")
        if op == "LIKE":
            val = f"%{val}%"
        clauses.append(f'"{col}" {op} ?')
        params.append(val)
    return ("WHERE " + " AND ".join(clauses) if clauses else "", params)


async def consultar_cubo(
    conn: AsyncConnection,
    cubo_id: uuid.UUID,
    *,
    dimensoes: list[str],
    medidas: list[str] | None = None,
    filtros: list[dict] | None = None,
    execucao_id: uuid.UUID | str | None = None,
) -> dict:
    cubo = await versionado.obter(
        conn, tabela="cubos", versoes="cubo_versoes", idcol="cubo_id", recurso_id=cubo_id
    )
    if cubo["numero"] == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="Cube has no published definition.", status=400
        )
    d = cubo["definicao"]
    dim_map = {x["nome"]: x["coluna"] for x in d.get("dimensoes", [])}
    med_map = {x["nome"]: x["expressao"] for x in d.get("medidas", [])}
    medidas = medidas or list(med_map)
    for dm in dimensoes:
        if dm not in dim_map:
            raise ErroAplicacao(
                codigo="dimensao_desconhecida",
                mensagem=f"Dimension '{dm}' does not exist in the cube.",
                status=400,
            )
    for md in medidas:
        if md not in med_map:
            raise ErroAplicacao(
                codigo="medida_desconhecida",
                mensagem=f"Measure '{md}' does not exist in the cube.",
                status=400,
            )

    origem = await _snapshot_fonte(conn, d["fonte"], execucao_id)
    onde, params = _where(filtros, dim_map)
    sel = [f'"{dim_map[dm]}" AS "{dm}"' for dm in dimensoes] + [
        f'({med_map[md]}) AS "{md}"' for md in medidas
    ]
    grupo = ", ".join(f'"{dim_map[dm]}"' for dm in dimensoes)
    sql = f"SELECT {', '.join(sel)} FROM read_parquet(?) {onde}" + (
        f" GROUP BY {grupo}" if grupo else ""
    )

    import duckdb

    duck = duckdb.connect()
    try:
        cur = duck.execute(sql, [origem["snapshot"], *params])
        cols = [c[0] for c in cur.description]
        linhas = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    except duckdb.Error as e:
        # Measure expression incompatible with the snapshot column (e.g. `sum` over text),
        # missing column, etc. It is an error in the cube DEFINITION, not in the platform: 400
        # with the engine detail, so the author can fix the measure/dimension.
        raise ErroAplicacao(
            codigo="consulta_invalida",
            mensagem="Could not query the cube: the definition does not match the snapshot data. "
            "Check the measure expressions and the dimension columns.",
            status=400,
            detalhes=[DetalheErro(campo="definicao", mensagem=str(e).strip())],
        ) from e
    finally:
        duck.close()
    return {
        "colunas": cols,
        "linhas": linhas,
        "dimensoes": dimensoes,
        "medidas": medidas,
        "origem": origem,
    }


async def consultar_view(
    conn: AsyncConnection,
    view_id: uuid.UUID,
    *,
    filtros_extra: list[dict] | None = None,
    execucao_id: uuid.UUID | str | None = None,
) -> dict:
    view = await versionado.obter(
        conn, tabela="views", versoes="view_versoes", idcol="view_id", recurso_id=view_id
    )
    if view["numero"] == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="View has no published definition.", status=400
        )
    vd = view["definicao"]
    dims = list(vd.get("linhas", [])) + list(vd.get("colunas", []))
    medidas = vd.get("valores", [])
    filtros = list(vd.get("filtros", []))
    liberadas = set(vd.get("filtros_liberados", []))
    for f in filtros_extra or []:  # the operator can only filter on RELEASED dimensions
        if f.get("dimensao") not in liberadas:
            raise ErroAplicacao(
                codigo="filtro_nao_liberado",
                mensagem=f"Filter not released: {f.get('dimensao')}.",
                status=403,
            )
        filtros.append(f)
    res = await consultar_cubo(
        conn,
        uuid.UUID(str(vd["cubo_id"])),
        dimensoes=dims,
        medidas=medidas,
        filtros=filtros,
        execucao_id=execucao_id,
    )
    res["layout"] = {
        "linhas": vd.get("linhas", []),
        "colunas": vd.get("colunas", []),
        "valores": medidas,
    }
    return res


async def consultar_pagina(
    conn: AsyncConnection,
    pagina_id: uuid.UUID,
    *,
    filtros_extra: list[dict] | None = None,
    execucao_id: uuid.UUID | str | None = None,
) -> dict:
    """Render a page: each block REFERENCES a view (the page does not compute).
    Page filters apply to the views' released dimensions. Returns blocks + the origin of each."""
    pagina = await versionado.obter(
        conn, tabela="paginas", versoes="pagina_versoes", idcol="pagina_id", recurso_id=pagina_id
    )
    if pagina["numero"] == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="Page has no published definition.", status=400
        )
    pd = pagina["definicao"]
    blocos = []
    for b in pd.get("blocos", []):
        res = await consultar_view(
            conn, uuid.UUID(str(b["view_id"])), filtros_extra=filtros_extra, execucao_id=execucao_id
        )
        blocos.append(
            {
                "titulo": b.get("titulo"),
                "tipo": b.get("tipo", "pivot"),
                "view_id": b["view_id"],
                "total": bool(
                    b.get("total", False)
                ),  # Total row in the footer (configurable per block)
                "resultado": res,
            }
        )
    return {"pagina": pagina["nome"], "versao": pagina["numero"], "blocos": blocos}
