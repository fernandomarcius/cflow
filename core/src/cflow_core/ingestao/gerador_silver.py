"""Generates a bronze->silver pipeline definition from the REAL bronze schema.

With dozens of datasets and over a thousand columns, writing typing by hand is not reasonable. But
what comes out of here is a **real pipeline**: versioned JSON, audited, editable in the Studio,
with the SQL in plain sight. Generation is a starting point, not a parallel black box; whoever
knows the domain refines it afterwards.

Inference decides **whether** a column is a number/date. **How** to convert comes from the family
profile (`perfis.py`), which is declared. The split exists because of fixed-width layouts:
`TRY_CAST` would get the type right and the value wrong by 100x, without failing.

Golden rule: a column is typed only when **100 % of non-null values convert** in the sample. The
rest stays `VARCHAR` with a `-- review` note. Typing little is deliberate: guessing types is
exactly what bronze avoids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import ErroAplicacao
from . import perfis as P
from .contrato import grao as ler_grao

# Columns injected when reading bronze. They are provenance, not source data: they enter silver with
# a `_` prefix so they do not collide with business columns.
#
# `ingestion_date` comes from hive partitioning (`key=value` in the path). `competencia` does NOT
# come on its own: the slice is a BARE directory, and `leitura.sql_leitura(competencia=True)` binds
# it from the bronze INDEX (the batch's `chave_lote`, one subquery per part), only for sources that
# declare `janela.fatia_e_competencia`. For the others the column does not exist in the bronze read,
# and the generator ignores it because it is not in the schema.
COLUNAS_PROVENIENCIA = ("ingestion_date", "competencia")
_NAO_ALFANUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ColunaSilver:
    origem: str
    destino: str
    tipo: str  # "data" | "numero" | "texto" (date | number | text)
    expressao: str
    motivo: str = ""
    # The column's real value (absence sentinel already as NULL). It is the denominator of the
    # typing criterion AND of the loss guard: without it, a `--` sentinel counts as "loss in cast".
    presente: str = ""


def normalizar_nome(bruto: str) -> str:
    """Source column name -> snake_case. `Due Dt./  Amt.` becomes `due_dt_amt`.

    Silver is consumed from outside: a name with spaces, dots or accents forces every consumer to
    quote it and disappears in tools that do not support it."""
    import unicodedata

    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", bruto) if unicodedata.category(c) != "Mn"
    )
    limpo = _NAO_ALFANUM.sub("_", sem_acento.lower()).strip("_")
    return limpo or "coluna"


def _converte_tudo(con, tabela: str, presente: str, expressao: str) -> bool:
    """Does the expression convert 100 % of the PRESENT values? That is the typing criterion.

    "Present" excludes the source's absence sentinel (`--`, `N/D`): it is not a value that failed
    conversion, it is the source saying there is no value. A column with no present value returns
    False: asserting a type without an example is a guess."""
    r = con.execute(
        f"SELECT count({presente}), count({expressao}) FROM {tabela}"  # noqa: S608
    ).fetchone()
    return bool(r[0]) and r[0] == r[1]


def _identificador_no_dado(con, tabela: str, presente: str) -> str:
    """Converts 100 % to a number and is STILL not a number? The data answers, the name does not.

    A column such as `Risk_Holder` can hold an unpunctuated tax ID (`00000000000000`): nothing in
    the name gives it away, and typing erases the leading zero, producing a different ID. Two
    objective signals, measured on the sample: a leading zero, and an integer too long to be a
    quantity (>= 12 digits, the same ruler the front-end grid uses to avoid thousands separators
    on tax IDs).

    Does not apply to fixed-width layouts (`padding_zeros`): there the zero is padding."""
    q = (
        "SELECT count(*) FILTER (WHERE regexp_matches(v, '^0[0-9]+$')), "
        "count(*) FILTER (WHERE regexp_matches(v, '^[0-9]{12,}$')) "
        f"FROM (SELECT {presente} AS v FROM {tabela}) WHERE v IS NOT NULL"  # noqa: S608
    )
    zeros, longos = con.execute(q).fetchone()
    if zeros:
        return "identifier: leading zero in the data"
    if longos:
        return "identifier: integer of 12+ digits"
    return ""


def _tipo_ja_na_origem(tipo_sql: str) -> str:
    """Bronze is **almost** always text. The exception is JSON APIs: a field such as `quotedAt`
    arrives as `TIMESTAMP` and Parquet preserves it. A column that already arrives typed has nothing
    to convert: applying `trim()` to it does not even compile, and recasting through text would be
    a gratuitous risk."""
    t = tipo_sql.upper()
    if t.startswith(("DATE", "TIMESTAMP")):
        return "data"
    if t.startswith(("DECIMAL", "DOUBLE", "FLOAT", "BIGINT", "INTEGER", "HUGEINT", "SMALLINT")):
        return "numero"
    return ""


def inferir_colunas(con, tabela: str, colunas: list[str], dataset: str) -> list[ColunaSilver]:
    """Classify each column of the sample. Conservative by construction."""
    perfil = P.perfil_de(dataset)
    tipos = {n: str(t) for n, t, *_ in con.execute(f"DESCRIBE {tabela}").fetchall()}  # noqa: S608
    out: list[ColunaSilver] = []
    for bruta in colunas:
        destino = normalizar_nome(bruta)
        alvo_cru = '"' + bruta.replace('"', '""') + '"'
        if bruta in COLUNAS_PROVENIENCIA:
            out.append(ColunaSilver(bruta, f"_{destino}", "texto", f'"{bruta}"', "lake provenance"))
            continue

        ja = _tipo_ja_na_origem(tipos.get(bruta, "VARCHAR"))
        if ja:
            out.append(ColunaSilver(bruta, destino, ja, alvo_cru, "already typed at source"))
            continue

        presente = P.expressao_presente(bruta, perfil)
        expr_data = P.expressao_data(bruta, perfil)
        if _converte_tudo(con, tabela, presente, expr_data):
            out.append(ColunaSilver(bruta, destino, "data", expr_data, presente=presente))
            continue

        if not P.pode_ser_numero(bruta, perfil):
            motivo = "identifier: keep as text"
        else:
            expr_num = P.expressao_numero(bruta, perfil, dataset=dataset)
            if not _converte_tudo(con, tabela, presente, expr_num):
                motivo = "does not convert 100%"
            else:
                motivo = (
                    "" if perfil.padding_zeros else _identificador_no_dado(con, tabela, presente)
                )
                if not motivo:
                    out.append(ColunaSilver(bruta, destino, "numero", expr_num, presente=presente))
                    continue

        out.append(ColunaSilver(bruta, destino, "texto", alvo_cru, motivo, presente=presente))
    return out


def _quotar(coluna: str) -> str:
    return '"' + coluna.replace('"', '""') + '"'


def montar_sql(colunas: list[ColunaSilver], alias: str, *, grao_saida: dict | None = None) -> str:
    """Typing SELECT, aligned and commented. This is what the specialist reads and adjusts in the Studio.

    `grao_saida` (already translated to silver names, see `grao_para_saida`) adds a `QUALIFY`
    clause when the grain declares a tiebreak: the source re-presents the same record in a later
    delivery, and of the copies only the one that wins the tiebreak survives.

    **`rank()`, never `row_number()`.** `row_number()` resolves any tie by picking an arbitrary
    row: two genuinely distinct rows tying on the tiebreak would collapse into one, silently and
    non-deterministically across runs. With `rank()` the tie SURVIVES; the `silver_grao_violado`
    guard sees it and refuses publication, handing back to the contract the case the declared
    tiebreak does not cover instead of hiding it."""
    if not colunas:
        projecao = f"SELECT * FROM {alias}"
    else:
        largura = max(len(c.expressao) for c in colunas)
        linhas = []
        for i, c in enumerate(colunas):
            virgula = "," if i < len(colunas) - 1 else ""
            comentario = f"  -- {c.motivo}" if c.motivo else ""
            linhas.append(f"  {c.expressao:<{largura}} AS {c.destino}{virgula}{comentario}")
        projecao = "SELECT\n" + "\n".join(linhas) + f"\nFROM {alias}"

    desempate = (grao_saida or {}).get("desempate")
    if not desempate:
        return projecao

    particao = ", ".join(_quotar(c) for c in grao_saida.get("chaves") or [])
    ordenacao = ", ".join(
        f"{_quotar(c['coluna'])} {str(c.get('ordem', 'asc')).upper()} NULLS LAST"
        for c in desempate.get("colunas") or []
    )
    motivo = desempate.get("motivo") or ""
    comentario = f"-- declared tiebreak: {motivo}\n" if motivo else ""
    return (
        f"{projecao}\n{comentario}"
        f"QUALIFY rank() OVER (PARTITION BY {particao} ORDER BY {ordenacao}) = 1"
    )


def coluna_silver_da_chave(chave: str) -> str:
    """The name, IN SILVER, of the column the contract declares as a grain key.

    The contract speaks in BRONZE column names (`competencia`); silver renames provenance with a
    leading `_` so it does not collide with business columns (`_competencia`). They are two sides
    of the same column, and anyone comparing the two names without translating reports a divergence
    that does not exist, or worse, looks for `competencia` in the published table, does not find
    it, and concludes the grain is broken.
    """
    return f"_{chave}" if chave in COLUNAS_PROVENIENCIA else chave


def grao_para_saida(grao_contrato: dict | None, colunas: list[ColunaSilver]) -> dict:
    """Translate the contract's `grao` into the block that travels on the **output** node.

    Why the grain ends up in the pipeline definition instead of being read from the contract at
    publish time: the publisher is the `saida` node, which has `dataset` and `conexao_id`, while
    `fonte_id` lives on the `fonte` node, on the other side of the graph. Copying the grain here
    makes the guard travel inside the versioned, audited JSON and stay visible in the Studio next
    to the rest of the publication config. The price is that the copy can diverge from the contract
    if someone edits one of them, so a consistency check should compare both.

    Absence becomes an explicit `desconhecido` (never "the keys are all the columns", which would
    make any table trivially unique). A declared key with no matching column in silver is a **loud
    error at generation time**: it is a registration typo, and letting it pass would produce a
    guard that never fires and looks like coverage."""
    g = ler_grao({"grao": grao_contrato})
    if not g.declarado:
        return {"estado": g.estado, "chaves": [], "motivo": g.motivo}

    destinos = {c.destino for c in colunas}
    chaves: list[str] = []
    for c in g.chaves:
        alvo = coluna_silver_da_chave(c)
        if alvo not in destinos:
            raise ErroAplicacao(
                codigo="silver_grao_sem_coluna",
                mensagem=(
                    f"The declared grain uses column '{c}', which does not exist in the generated "
                    f"silver (looked up as '{alvo}'). Fix 'grao.chaves' in the source contract."
                ),
                status=400,
            )
        chaves.append(alvo)

    saida: dict = {"estado": "declarado", "chaves": chaves, "motivo": g.motivo}
    if g.desempate:
        colunas_desempate: list[dict] = []
        for nome, ordem in g.desempate:
            alvo = coluna_silver_da_chave(nome)
            if alvo not in destinos:
                raise ErroAplicacao(
                    codigo="silver_grao_sem_coluna",
                    mensagem=(
                        f"The grain tiebreak uses column '{nome}', which does not exist in the "
                        f"generated silver (looked up as '{alvo}'). Fix 'grao.desempate' in the "
                        "source contract."
                    ),
                    status=400,
                )
            colunas_desempate.append({"coluna": alvo, "ordem": ordem})
        saida["desempate"] = {"colunas": colunas_desempate, "motivo": g.desempate_motivo}
    return saida


def definicao_pipeline(
    *,
    fonte_id: str,
    dataset_bronze: str,
    dataset_silver: str,
    colunas: list[ColunaSilver],
    chave_max: str | None = None,
    conexao_id: str | None = None,
    destino: str = "iceberg",
    grao: dict | None = None,
) -> dict:
    """3-node graph: lake source -> sql (typing) -> silver output.

    `conexao_id` is the lake's object connection, the SAME one the bronze was read from. Omitting
    it makes the output fall back to the local backend (the air-gapped default): the node writes to
    the container disk, finishes **without error** and nothing shows up in the bucket. That is why
    it is declared in the versioned definition instead of left implicit.

    `grao` is the `grao` block of the source CONTRACT (bronze column names); it is translated to
    silver names and stored in the output node's `config.grao`, see `grao_para_saida`."""
    cfg_fonte: dict = {"origem": "lake", "fonte_id": fonte_id}
    if chave_max:
        cfg_fonte["chave_max"] = chave_max
    # `iceberg` is the default: consumers (query engines) read tables, not files. `lake` (Parquet +
    # pointer) remains available for those who want the file.
    cfg_saida: dict = {"destino": destino, "dataset": dataset_silver}
    if conexao_id:
        cfg_saida["conexao_id"] = conexao_id
    # Only when there is something to declare. Absence and `{"estado": "desconhecido"}` mean the
    # SAME thing to readers (`contrato.grao` normalizes one into the other), and writing the empty
    # block for every dataset without a measured grain only bloats the versioned definition. The
    # publisher reads from here (`iceberg.publicar(grao=...)`).
    g_saida = grao_para_saida(grao, colunas)
    if g_saida["estado"] != "desconhecido":
        cfg_saida["grao"] = g_saida
    return {
        "nos": [
            {
                "id": "bronze",
                "tipo": "fonte",
                "rotulo": f"bronze · {dataset_bronze}",
                "config": cfg_fonte,
            },
            {
                "id": "tipado",
                "tipo": "sql",
                "rotulo": "typing",
                "sql": montar_sql(colunas, '"bronze"', grao_saida=g_saida),
                "descricao": (
                    "Typing generated from the real bronze schema. Columns without a declared "
                    "type stay as text; review the marked ones."
                ),
            },
            {
                "id": "silver",
                "tipo": "saida",
                "rotulo": f"silver · {dataset_silver}",
                "config": cfg_saida,
            },
        ],
        "arestas": [{"de": "bronze", "para": "tipado"}, {"de": "tipado", "para": "silver"}],
    }
