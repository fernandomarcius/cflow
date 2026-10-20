"""Summary of the view and cube lists, in one SQL query per listing.

The Studio lists show, per row, what each item IS (source cube, how many dimensions, measures
and filters), which version is published and whether there is a draft ahead of it, and who
depends on it (pages that display the view; views that read the cube). Without this summary the
front end assembled all of that with `GET /{collection}/{id}` + `GET /{collection}/{id}/versoes`
PER ITEM, plus a `GET /paginas/{id}` per published page, which means hundreds of calls on a
single navigation. Here it is one round trip and one query.

The rules are the same the front end applied over the individual responses, so the screen does
not change:

- the DEFINITION read is the LATEST version's (the one `GET /{collection}/{id}` without `numero`
  returns, which is what the user reopens to edit), not the published one;
- `publicada` is the active version (`versao_publicada_id`); `ultimo` is the highest number; a
  draft is ahead when `ultimo > publicada.numero`;
- the count of a list that is not a list is 0 (the front end did
  `Array.isArray(x) ? x.length : 0`);
- visibility follows the listing: a non-admin does not see an item flagged `sistema`, and that
  also applies to what is COUNTED (views that depend on a cube, pages that display a view). A
  published system page enters everyone's catalog, but a non-admin cannot read its definition:
  it is not counted but reported separately (`paginas_nao_verificadas`), as the screen already
  said.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _n(expr: str) -> str:
    """Length of a jsonb array; 0 if it is not an array (or is missing)."""
    return f"CASE WHEN jsonb_typeof({expr}) = 'array' THEN jsonb_array_length({expr}) ELSE 0 END"


def _publicada(numero, criado_em) -> dict | None:
    if numero is None:
        return None
    return {"numero": numero, "criado_em": criado_em.isoformat() if criado_em else None}


# Latest version of each resource (the highest number). DISTINCT ON uses the (resource, numero) index.
def _ultima(versoes: str, idcol: str) -> str:
    return (
        f"SELECT DISTINCT ON ({idcol}) {idcol} AS rid, numero, definicao "
        f"FROM {versoes} ORDER BY {idcol}, numero DESC"
    )


_SQL_VIEWS = f"""
WITH ult AS ({_ultima("view_versoes", "view_id")}),
pagina_ult AS (
  SELECT DISTINCT ON (pv.pagina_id) pv.pagina_id, pv.definicao
  FROM pagina_versoes pv
  JOIN paginas p ON p.id = pv.pagina_id
  WHERE p.versao_publicada_id IS NOT NULL AND (:adm OR NOT p.sistema)
  ORDER BY pv.pagina_id, pv.numero DESC
),
uso AS (
  -- A block stores the view in `view_id` (or `vid`, the legacy name). Twice on the same page counts 1.
  SELECT COALESCE(b ->> 'view_id', b ->> 'vid') AS vid, count(DISTINCT pu.pagina_id) AS paginas
  FROM pagina_ult pu
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(pu.definicao -> 'blocos') = 'array'
         THEN pu.definicao -> 'blocos' ELSE '[]'::jsonb END
  ) AS b
  GROUP BY 1
),
ocultas AS (
  SELECT count(*) AS n FROM paginas
  WHERE versao_publicada_id IS NOT NULL AND sistema AND NOT :adm
)
SELECT v.id, v.nome, v.sistema,
       COALESCE(ult.numero, 0) AS ultimo,
       NULLIF(ult.definicao ->> 'cubo_id', '') AS cubo_id,
       {_n("ult.definicao -> 'linhas'")} AS linhas,
       {_n("ult.definicao -> 'colunas'")} AS colunas,
       {_n("ult.definicao -> 'valores'")} AS valores,
       {_n("ult.definicao -> 'filtros_liberados'")} AS filtros_liberados,
       pub.numero AS pub_numero, pub.criado_em AS pub_em,
       COALESCE(uso.paginas, 0) AS paginas,
       ocultas.n AS paginas_nao_verificadas
FROM views v
LEFT JOIN ult ON ult.rid = v.id
LEFT JOIN view_versoes pub ON pub.id = v.versao_publicada_id
LEFT JOIN uso ON uso.vid = v.id::text
CROSS JOIN ocultas
WHERE :adm OR NOT v.sistema
ORDER BY v.criado_em
"""


async def listar_views(conn: AsyncConnection, *, incluir_sistema: bool) -> list[dict]:
    """`GET /views?resumo=true`: the usual listing + a per-view `resumo`, in one query."""
    rows = (await conn.execute(text(_SQL_VIEWS), {"adm": incluir_sistema})).mappings().all()
    return [
        {
            "id": str(x["id"]),
            "nome": x["nome"],
            "sistema": bool(x["sistema"]),
            "resumo": {
                "cubo_id": x["cubo_id"],
                "linhas": x["linhas"],
                "colunas": x["colunas"],
                "valores": x["valores"],
                "filtros_liberados": x["filtros_liberados"],
                "ultimo": x["ultimo"],
                "publicada": _publicada(x["pub_numero"], x["pub_em"]),
                "paginas": x["paginas"],
                "paginas_nao_verificadas": x["paginas_nao_verificadas"],
            },
        }
        for x in rows
    ]


_SQL_CUBOS = f"""
WITH ult AS ({_ultima("cubo_versoes", "cubo_id")}),
view_ult AS (
  SELECT DISTINCT ON (vv.view_id) vv.view_id, vv.definicao ->> 'cubo_id' AS cubo_id
  FROM view_versoes vv
  JOIN views v ON v.id = vv.view_id
  WHERE :adm OR NOT v.sistema
  ORDER BY vv.view_id, vv.numero DESC
),
dependentes AS (
  SELECT cubo_id, count(*) AS views FROM view_ult WHERE cubo_id IS NOT NULL GROUP BY cubo_id
)
SELECT c.id, c.nome, c.sistema,
       COALESCE(ult.numero, 0) AS ultimo,
       {_n("ult.definicao -> 'dimensoes'")} AS dimensoes,
       {_n("ult.definicao -> 'medidas'")} AS medidas,
       NULLIF(ult.definicao -> 'fonte' ->> 'pipeline_id', '') AS pipeline_id,
       NULLIF(ult.definicao -> 'fonte' ->> 'no_id', '') AS no_id,
       pub.numero AS pub_numero, pub.criado_em AS pub_em,
       COALESCE(dep.views, 0) AS views
FROM cubos c
LEFT JOIN ult ON ult.rid = c.id
LEFT JOIN cubo_versoes pub ON pub.id = c.versao_publicada_id
LEFT JOIN dependentes dep ON dep.cubo_id = c.id::text
WHERE :adm OR NOT c.sistema
ORDER BY c.criado_em
"""


async def listar_cubos(conn: AsyncConnection, *, incluir_sistema: bool) -> list[dict]:
    """`GET /cubos?resumo=true`: the usual listing + a per-cube `resumo`, in one query."""
    rows = (await conn.execute(text(_SQL_CUBOS), {"adm": incluir_sistema})).mappings().all()
    return [
        {
            "id": str(x["id"]),
            "nome": x["nome"],
            "sistema": bool(x["sistema"]),
            "resumo": {
                "dimensoes": x["dimensoes"],
                "medidas": x["medidas"],
                "pipeline_id": x["pipeline_id"],
                "no_id": x["no_id"],
                "ultimo": x["ultimo"],
                "publicada": _publicada(x["pub_numero"], x["pub_em"]),
                "views": x["views"],
            },
        }
        for x in rows
    ]
