"""`escrita_sql` effect node: pure PLAN BUILDING (what preview and execution show/run) kept apart
from the EFFECT (the write to the external target). The SAME SQL/plan is built on both paths:
preview shows it, execution fires it.

- `montar_plano(cfg, colunas_pred, tipo)` - pure, no I/O: resolves target columns/projection,
  strategy and the final SQL that would run (per driver). Used by preview (dry-run) and execution.
- `executar_escrita(conector, config, senha, plano, ...)` - effect: projects the rows and calls the
  write connector (credential/flag already resolved and re-checked by the caller).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..conexoes.conectores.base import conferir_escopo, valores_do_escopo
from ..conexoes.conectores.postgres import montar_sql_limpeza_pg, montar_sql_tabela_pg
from ..conexoes.conectores.sqlserver import montar_sql_limpeza_mssql, montar_sql_tabela_mssql
from ..errors import ErroAplicacao

_ENTRADA = re.compile(r"\b_entrada\b")

# Schema of the one-row SUMMARY table an effect node materializes as its snapshot. Single source of
# the format, shared by execution (engine) and preview (dry-run) so the two stay in sync.
COLUNAS_RESUMO = (
    "destino VARCHAR, estrategia VARCHAR, linhas_entrada BIGINT, "
    "linhas_afetadas BIGINT, status VARCHAR, duracao_ms BIGINT"
)


def criar_tabela_resumo(duck, nome: str) -> None:
    """(Re)create the effect node's summary table in DuckDB. Same schema in preview and execution,
    so the dry-run schema equals the execution one."""
    duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
    duck.execute(f'CREATE TABLE "{nome}" ({COLUNAS_RESUMO})')


@dataclass
class PlanoEscrita:
    modo: str  # "tabela" | "sql"
    destino: str  # "schema.table" (tabela) | "(DML)" (sql)
    sql_gerado: str  # final SQL that runs on the target - what preview shows
    schema: str | None = None
    tabela: str | None = None
    colunas_destino: list[str] = field(default_factory=list)
    projecao: list[tuple[str, str]] = field(
        default_factory=list
    )  # (source, target) - tabela mode only
    estrategia: str | None = None
    chave: list[str] | None = None
    lote_tamanho: int = 1000
    sql: str | None = None  # sql mode: the DML
    usa_entrada: bool = False
    # `limpar_escopo`: {target column: ':parameter'}. The connector deletes that scope and writes in
    # the SAME transaction. `sql_limpeza` is the built DELETE (preview shows it ahead of the write,
    # in `sql_gerado`).
    escopo: dict[str, str] | None = None
    sql_limpeza: str | None = None

    @property
    def idempotente(self) -> bool:
        """Does repeating the effect converge to the SAME target state? upsert/truncate_insert yes;
        append only when it replaces the scope (delete and write in one transaction, so the 2nd run
        undoes the 1st); free-form DML never. This is the criterion for the re-entry fail-safe
        (`_marcar_efeito_ou_recusar`) and the re-run warning: read it from here instead of
        repeating the strategy list."""
        if self.modo != "tabela":
            return False
        if self.estrategia in ("upsert", "truncate_insert"):
            return True
        return self.estrategia == "append" and self.escopo is not None


def _builder(tipo: str):
    return montar_sql_tabela_mssql if tipo == "sqlserver" else montar_sql_tabela_pg


def _builder_limpeza(tipo: str):
    return montar_sql_limpeza_mssql if tipo == "sqlserver" else montar_sql_limpeza_pg


def montar_plano(cfg: dict, colunas_pred: list[str], tipo: str) -> PlanoEscrita:
    """Build the write plan (pure). `colunas_pred` = predecessor columns (for same-name mapping and
    the `_entrada` staging). `tipo` = connection type (postgres|sqlserver) -> SQL dialect."""
    modo = cfg.get("modo")
    if modo == "tabela":
        schema, tabela = cfg["schema"], cfg["tabela"]
        mapeamento = cfg.get("mapeamento")
        if mapeamento:
            # Every SOURCE column of the mapping must exist in the input, otherwise the projection
            # would silently write NULL (typo in `de` / renamed upstream column). Fail early; this
            # also runs in preview (dry-run), so the error shows before publishing/executing.
            faltando = [m["de"] for m in mapeamento if m["de"] not in colunas_pred]
            if faltando:
                raise ErroAplicacao(
                    codigo="escrita_coluna_ausente",
                    mensagem=(
                        "Mapping source columns missing from the input: "
                        + ", ".join(faltando)
                        + "."
                    ),
                    status=400,
                )
            projecao = [(m["de"], m["para"]) for m in mapeamento]
        else:  # no mapping = same-name predecessor columns
            projecao = [(c, c) for c in colunas_pred]
        colunas_destino = [d for _, d in projecao]
        estrategia = cfg.get("estrategia", "append")
        chave = cfg.get("chave")
        sql_gerado = _builder(tipo)(schema, tabela, colunas_destino, estrategia, chave)
        escopo = None
        sql_limpeza = None
        if "limpar_escopo" in cfg:
            # Same refusals as the connector, raised here so they show in preview: empty scope,
            # incompatible strategy, and a scope column the write does not write.
            escopo = cfg["limpar_escopo"]
            conferir_escopo(escopo, estrategia, colunas_destino)
            sql_limpeza = _builder_limpeza(tipo)(schema, tabela, escopo)
            sql_gerado = f"{sql_limpeza};\n{sql_gerado}"
        return PlanoEscrita(
            modo="tabela",
            destino=f"{schema}.{tabela}",
            sql_gerado=sql_gerado,
            schema=schema,
            tabela=tabela,
            colunas_destino=colunas_destino,
            projecao=projecao,
            estrategia=estrategia,
            chave=chave,
            lote_tamanho=int(cfg.get("lote_tamanho", 1000) or 1000),
            escopo=escopo,
            sql_limpeza=sql_limpeza,
        )
    if "limpar_escopo" in cfg:
        # In free-form DML the field would have no effect, and a config silently accepted that does
        # nothing looks configured but is not (the expected cleanup would never happen).
        raise ErroAplicacao(
            codigo="escrita_escopo_invalido",
            mensagem="limpar_escopo only applies in 'tabela' mode.",
            status=400,
        )
    sql = cfg.get("sql") or ""
    return PlanoEscrita(
        modo="sql",
        destino="(DML)",
        sql_gerado=sql,
        sql=sql,
        usa_entrada=bool(_ENTRADA.search(sql)),
    )


async def executar_escrita(
    conector,
    config: dict,
    senha: str | None,
    plano: PlanoEscrita,
    *,
    colunas_pred: list[str],
    linhas_pred: list[dict],
    params: dict | None,
) -> dict:
    """Fire the effect and return the one-row summary that becomes the node snapshot. The write
    `conector` and the credential arrive already resolved, with the flag re-checked by the caller
    (engine)."""
    linhas_entrada = len(linhas_pred)
    apagadas = None
    if plano.modo == "tabela":
        # Project as a GENERATOR (not a 2nd list): the connector walks `linhas` once to build the
        # records, avoiding linhas_pred + linhas_dest + records alive at the same time.
        linhas_dest = ({d: lin.get(o) for (o, d) in plano.projecao} for lin in linhas_pred)
        if plano.escopo is not None:
            # Scope values are the run's NORMALIZED parameters and only they reach the connector
            # (nothing else enters the DELETE). A missing value is refused loudly HERE, before
            # opening a connection to the target.
            valores = valores_do_escopo(plano.escopo, params)
            r = await conector.escrever_tabela_no_escopo(
                config,
                senha,
                schema=plano.schema,
                tabela=plano.tabela,
                colunas_destino=plano.colunas_destino,
                linhas=linhas_dest,
                estrategia=plano.estrategia or "append",
                chave=plano.chave,
                lote_tamanho=plano.lote_tamanho,
                escopo=plano.escopo,
                params=valores,
            )
            afetadas, apagadas = r["linhas_afetadas"], r["linhas_apagadas"]
        else:
            afetadas = await conector.escrever_tabela(
                config,
                senha,
                schema=plano.schema,
                tabela=plano.tabela,
                colunas_destino=plano.colunas_destino,
                linhas=linhas_dest,
                estrategia=plano.estrategia or "append",
                chave=plano.chave,
                lote_tamanho=plano.lote_tamanho,
            )
        estrategia = plano.estrategia or "append"
    else:
        entrada = (colunas_pred, linhas_pred) if plano.usa_entrada else None
        afetadas = await conector.executar_dml(config, senha, plano.sql or "", params, entrada)
        estrategia = "sql"
    return {
        "destino": plano.destino,
        "estrategia": estrategia,
        "linhas_entrada": linhas_entrada,
        "linhas_afetadas": int(afetadas),
        # How many rows of the previous run `limpar_escopo` replaced (None without scope): the number
        # that shows, from the run alone, that re-writing swapped the scope instead of adding.
        "linhas_apagadas": None if apagadas is None else int(apagadas),
    }
