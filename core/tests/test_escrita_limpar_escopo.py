"""`limpar_escopo` on the `escrita_sql` node -- pure, no database.

The defect this closes: scope cleanup (an `escrita_sql` node in `sql` mode with a DELETE) and the
write (another node, with MERGE) ran in SEPARATE TRANSACTIONS. Two paths left a tail from the
previous run: re-running "from" the write node (the cleanup stays upstream, copied as reused, and
only the MERGE runs) and two runs of the same scope interleaving DELETE/MERGE. With `limpar_escopo`,
the scope DELETE and the write are ONE business statement: same connection, same transaction, a
single commit.

Here: SQL assembly (SQL Server with HOLDLOCK, Postgres with cast and lock), definition validation,
value slicing (a parameter without a value is a loud refusal -- never a DELETE without WHERE nor a
silent `= NULL`) and the ORDER of operations inside the SQL Server transaction, with a fake
connection that returns rows in pytds format.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from pytds.row_strategies import dict_row_strategy

from cflow_core.conexoes.conectores import sqlserver as mssql
from cflow_core.conexoes.conectores.postgres import (
    montar_sql_fora_do_escopo_pg,
    montar_sql_limpeza_pg,
    montar_sql_tabela_pg,
    montar_sql_trava_escopo_pg,
)
from cflow_core.conexoes.conectores.sqlserver import (
    montar_sql_fora_do_escopo_mssql,
    montar_sql_limpeza_mssql,
    montar_sql_tabela_mssql,
)
from cflow_core.conexoes.parametros import NUMERADO, PYFORMAT, vincular
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao.no_escrita_sql import executar_escrita, montar_plano
from cflow_core.pipelines.definicao import validar

ESCOPO = {"unitId": ":operacao_id", "dataReferencia": ":competencia_inicio"}

# ---------------- montagem: SQL Server ----------------


def test_mssql_limpeza_tem_holdlock_e_bind_por_parametro():
    sql = montar_sql_limpeza_mssql("dbo", "CFLOW_X", ESCOPO)
    assert sql == (
        "DELETE FROM [dbo].[CFLOW_X] WITH (HOLDLOCK) "
        "WHERE [unitId] = :operacao_id AND [dataReferencia] = :competencia_inicio"
    )
    # The SAME path as sql mode: `vincular` swaps `:name` for the pytds placeholder and the value
    # goes outside the text, in predicate order.
    sql_b, valores = vincular(
        sql, {"operacao_id": "188", "competencia_inicio": "2026-08-01"}, PYFORMAT
    )
    assert sql_b == (
        "DELETE FROM [dbo].[CFLOW_X] WITH (HOLDLOCK) WHERE [unitId] = %s AND [dataReferencia] = %s"
    )
    assert valores == ["188", "2026-08-01"]


def test_mssql_fora_do_escopo_conta_nulo_como_fora():
    sql = montar_sql_fora_do_escopo_mssql({"op": ":operacao_id", "dt": ":data_registro"})
    assert sql == (
        "SELECT COUNT(*) AS n FROM #stage "
        "WHERE NOT ([op] = :operacao_id AND [dt] = :data_registro) OR [op] IS NULL OR [dt] IS NULL"
    )


# ---------------- montagem: Postgres ----------------


def test_pg_limpeza_com_cast_do_tipo_do_alvo():
    sql = montar_sql_limpeza_pg("s", "t", {"op": ":operacao_id"}, {"op": "integer"})
    assert sql == 'DELETE FROM "s"."t" WHERE "op" = :operacao_id::text::integer'
    sql_b, valores = vincular(sql, {"operacao_id": "1"}, NUMERADO)
    assert sql_b == 'DELETE FROM "s"."t" WHERE "op" = $1::text::integer'
    assert valores == ["1"]


def test_pg_limpeza_sem_tipos_sai_sem_cast():
    # Preview (dry-run) does not read the target catalog: same rule as the final builder.
    assert montar_sql_limpeza_pg("s", "t", {"op": ":operacao_id"}) == (
        'DELETE FROM "s"."t" WHERE "op" = :operacao_id'
    )


def test_pg_trava_canoniza_o_valor_pelo_tipo_do_alvo():
    # The lock key is the value ALREADY CONVERTED to the column type: '0188' and '188' are the same
    # scope in the database and must contend for the same lock.
    sql = montar_sql_trava_escopo_pg("s", "t", {"op": ":operacao_id"}, {"op": "integer"})
    assert sql == (
        "SELECT pg_advisory_xact_lock(hashtext(concat_ws('|', 's.t', "
        "'op', (:operacao_id::text::integer)::text)))"
    )
    sql_b, _ = vincular(sql, {"operacao_id": "0188"}, NUMERADO)
    assert "($1::text::integer)::text" in sql_b


def test_pg_fora_do_escopo_trata_nulo_como_fora():
    sql = montar_sql_fora_do_escopo_pg({"op": ":operacao_id"}, {"op": "integer"})
    assert sql == (
        'SELECT count(*) FROM _stage WHERE NOT COALESCE(("op"::integer = '
        ":operacao_id::text::integer), false)"
    )


# ---------------- montar_plano ----------------


def _cfg(**extra) -> dict:
    return {
        "conexao_id": "c1",
        "modo": "tabela",
        "schema": "dbo",
        "tabela": "t",
        "estrategia": "upsert",
        "chave": ["op", "n"],
        **extra,
    }


def test_plano_com_escopo_mostra_o_delete_antes_da_gravacao():
    p = montar_plano(_cfg(limpar_escopo={"op": ":operacao_id"}), ["op", "n", "c"], "sqlserver")
    assert p.escopo == {"op": ":operacao_id"}
    assert p.sql_limpeza == montar_sql_limpeza_mssql("dbo", "t", {"op": ":operacao_id"})
    # The preview shows what runs: the scope DELETE, then the MERGE -- in that order.
    assert p.sql_gerado.index("DELETE") < p.sql_gerado.index("MERGE")
    assert p.sql_gerado.endswith(
        montar_sql_tabela_mssql("dbo", "t", ["op", "n", "c"], "upsert", ["op", "n"])
    )


def test_plano_sem_escopo_fica_como_antes():
    p = montar_plano(_cfg(), ["op", "n"], "postgres")
    assert p.escopo is None and p.sql_limpeza is None
    assert p.sql_gerado == montar_sql_tabela_pg("dbo", "t", ["op", "n"], "upsert", ["op", "n"])


def test_plano_coluna_do_escopo_fora_das_gravadas_falha():
    # A scope column that is not WRITTEN: rows enter without the scope value, the next run's DELETE
    # does not reach them, and they become a permanent tail. Fails at assembly (holds in preview).
    with pytest.raises(ErroAplicacao) as exc:
        montar_plano(_cfg(limpar_escopo={"projectId": ":operacao_id"}), ["op", "n"], "sqlserver")
    assert exc.value.codigo == "escrita_escopo_fora_das_colunas"
    assert "projectId" in exc.value.mensagem


def test_plano_idempotencia_considera_o_escopo():
    # an append that replaces the scope converges like upsert: repeating does not duplicate.
    assert montar_plano(
        _cfg(estrategia="append", chave=None, limpar_escopo={"op": ":operacao_id"}),
        ["op", "n"],
        "postgres",
    ).idempotente
    assert not montar_plano(
        _cfg(estrategia="append", chave=None), ["op", "n"], "postgres"
    ).idempotente
    assert montar_plano(_cfg(), ["op", "n"], "postgres").idempotente


# ---------------- definition validation ----------------

PARAMS = [
    {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
    {"nome": "competencia", "tipo": "competencia", "obrigatorio": True},
    {"nome": "opcional", "tipo": "texto"},
    {"nome": "senha_api", "tipo": "texto", "obrigatorio": True, "segredo": True},
]


def _def(cfg: dict, parametros=PARAMS) -> dict:
    return {
        "parametros": parametros,
        "nos": [{"id": "w", "tipo": "escrita_sql", "config": cfg}],
        "arestas": [],
    }


def _recusa(definicao: dict) -> str:
    with pytest.raises(ErroAplicacao) as exc:
        validar(definicao)
    assert exc.value.codigo == "grafo_invalido"
    return " | ".join(d.mensagem for d in exc.value.detalhes)


def test_valida_escopo_com_parametro_declarado_e_expandido():
    # `competencia` expands into `competencia_inicio`/`_fim`: that is the name the SQL binds.
    validar(
        _def(_cfg(limpar_escopo={"op": ":operacao_id", "dataReferencia": ":competencia_inicio"}))
    )
    validar(_def(_cfg(estrategia="append", chave=None, limpar_escopo={"op": ":operacao_id"})))


def test_recusa_escopo_com_truncate_insert():
    msg = _recusa(
        _def(_cfg(estrategia="truncate_insert", chave=None, limpar_escopo={"op": ":operacao_id"}))
    )
    assert "limpar_escopo" in msg and "truncate_insert" in msg


def test_recusa_escopo_no_modo_sql():
    cfg = {
        "conexao_id": "c1",
        "modo": "sql",
        "sql": "DELETE FROM x WHERE op = :operacao_id",
        "limpar_escopo": {"op": ":operacao_id"},
    }
    assert "limpar_escopo" in _recusa(_def(cfg))


@pytest.mark.parametrize("escopo", [{}, [], "op", None])
def test_recusa_escopo_vazio_ou_fora_de_formato(escopo):
    # Explicit `None` too: a scope "turned off" by mistake would delete nothing and write on top,
    # which is the behavior the field exists to prevent -- better not to accept the ambiguity.
    assert "limpar_escopo" in _recusa(_def(_cfg(limpar_escopo=escopo)))


def test_recusa_coluna_que_nao_e_identificador():
    assert "limpar_escopo" in _recusa(_def(_cfg(limpar_escopo={"op; DROP": ":operacao_id"})))


@pytest.mark.parametrize("valor", ["188", ":", "operacao_id", ":a b", 5, ":operacao_id OR 1=1"])
def test_recusa_valor_que_nao_e_um_parametro(valor):
    assert "limpar_escopo" in _recusa(_def(_cfg(limpar_escopo={"op": valor})))


def test_recusa_parametro_nao_declarado():
    msg = _recusa(_def(_cfg(limpar_escopo={"op": ":nao_existe"})))
    assert "nao_existe" in msg


def test_recusa_competencia_crua_que_nao_e_vinculavel():
    # `competencia` is not a bind (only `_inicio`/`_fim` are): accepting it would make the DELETE never match.
    assert "competencia" in _recusa(_def(_cfg(limpar_escopo={"dt": ":competencia"})))


def test_recusa_parametro_opcional():
    # A scope parameter must be required: blank has to be a loud error.
    msg = _recusa(_def(_cfg(limpar_escopo={"op": ":opcional"})))
    assert "opcional" in msg


def test_recusa_parametro_segredo():
    assert "senha_api" in _recusa(_def(_cfg(limpar_escopo={"op": ":senha_api"})))


def test_recusa_coluna_fora_do_mapeamento():
    cfg = _cfg(
        mapeamento=[{"de": "x", "para": "n"}, {"de": "y", "para": "c"}],
        chave=["n"],
        limpar_escopo={"op": ":operacao_id"},
    )
    msg = _recusa(_def(cfg))
    assert "op" in msg and "mapeamento" in msg


# ---------------- executar_escrita: value slicing ----------------


class _ConectorFalso:
    def __init__(self):
        self.chamadas: list[dict] = []

    async def escrever_tabela_no_escopo(self, config, senha, **kw):
        self.chamadas.append(kw)
        return {"linhas_afetadas": 2, "linhas_apagadas": 5}

    async def escrever_tabela(self, config, senha, **kw):
        self.chamadas.append({"sem_escopo": True, **kw})
        return 2


def _plano_escopo():
    return montar_plano(_cfg(limpar_escopo={"op": ":operacao_id"}), ["op", "n"], "postgres")


async def test_executar_leva_escopo_e_valores_ao_conector():
    fake = _ConectorFalso()
    resumo = await executar_escrita(
        fake,
        {},
        None,
        _plano_escopo(),
        colunas_pred=["op", "n"],
        linhas_pred=[{"op": 1, "n": 1}, {"op": 1, "n": 2}],
        params={"operacao_id": 1, "outro": "x"},
    )
    assert resumo["linhas_apagadas"] == 5 and resumo["linhas_afetadas"] == 2
    (kw,) = fake.chamadas
    assert kw["escopo"] == {"op": ":operacao_id"}
    # Only what the scope uses goes to the connector -- the other parameters stay out of the DELETE.
    assert kw["params"] == {"operacao_id": 1}
    assert list(kw["linhas"]) == [{"op": 1, "n": 1}, {"op": 1, "n": 2}]


@pytest.mark.parametrize("params", [None, {}, {"operacao_id": None}, {"operacao_id": ""}])
async def test_escopo_sem_valor_recusa_antes_de_tocar_o_destino(params):
    fake = _ConectorFalso()
    with pytest.raises(ErroAplicacao) as exc:
        await executar_escrita(
            fake,
            {},
            None,
            _plano_escopo(),
            colunas_pred=["op", "n"],
            linhas_pred=[{"op": 1, "n": 1}],
            params=params,
        )
    assert exc.value.codigo == "escrita_escopo_sem_valor"
    assert "operacao_id" in exc.value.mensagem
    assert fake.chamadas == []  # never reached the target


async def test_sem_escopo_resumo_diz_que_nada_foi_apagado():
    fake = _ConectorFalso()
    resumo = await executar_escrita(
        fake,
        {},
        None,
        montar_plano(_cfg(), ["op", "n"], "postgres"),
        colunas_pred=["op", "n"],
        linhas_pred=[{"op": 1, "n": 1}],
        params=None,
    )
    assert resumo["linhas_apagadas"] is None
    assert fake.chamadas[0].get("sem_escopo") is True


# ---------------- SQL Server: order inside the transaction (fake connection) ----------------


class _CursorFalso:
    """Mimics pytds with `as_dict=True`: the row is a dict keyed by column NAME, and an unnamed
    column becomes the key `"0"` (`dict_row_strategy`) -- not the integer index 0."""

    def __init__(self, log: list, respostas: list):
        self.log = log
        self.respostas = respostas
        self._ultimo = ""

    def execute(self, sql, params=()):
        self.log.append(("execute", sql, tuple(params or ())))
        self._ultimo = sql

    def executemany(self, sql, registros):
        self.log.append(("executemany", sql, len(list(registros))))

    def fetchone(self):
        m = re.search(r"\bAS\s+(\w+)\s*$", self._ultimo)
        return dict_row_strategy([m.group(1) if m else ""])([self.respostas.pop(0)])


class _ConnFalsa:
    def __init__(self, respostas: list):
        self.log: list = []
        self.commits = 0
        self.rollbacks = 0
        self._cur = _CursorFalso(self.log, respostas)

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1
        self.log.append(("commit",))

    def rollback(self):
        self.rollbacks += 1
        self.log.append(("rollback",))

    def close(self):
        pass


@pytest.fixture
def conexao_falsa(monkeypatch):
    estado: dict = {"autocommit": [], "conn": None}

    def _abrir(respostas):
        estado["conn"] = _ConnFalsa(respostas)

        def _conectar(config, senha, query_timeout=None, autocommit=True):
            estado["autocommit"].append(autocommit)
            return estado["conn"]

        monkeypatch.setattr(mssql, "_conectar", _conectar)
        return estado

    monkeypatch.setattr(
        mssql, "get_settings", lambda: SimpleNamespace(conexao_query_timeout_segundos=30)
    )
    return _abrir


async def _gravar_mssql(params=None, estrategia="upsert"):
    return await mssql.ConectorSqlServer().escrever_tabela_no_escopo(
        {},
        None,
        schema="dbo",
        tabela="t",
        colunas_destino=["op", "n"],
        linhas=[{"op": 1, "n": 1}, {"op": 1, "n": 2}],
        estrategia=estrategia,
        chave=["op", "n"] if estrategia == "upsert" else None,
        lote_tamanho=1000,
        escopo={"op": ":operacao_id"},
        params={"operacao_id": 1} if params is None else params,
    )


async def test_mssql_delete_do_escopo_e_merge_numa_transacao_so(conexao_falsa):
    estado = conexao_falsa([0, 5, 2])  # 0 rows outside scope, 5 deleted, 2 written
    res = await _gravar_mssql()
    assert res == {"linhas_afetadas": 2, "linhas_apagadas": 5}
    assert estado["autocommit"] == [False]  # one connection, explicit transaction
    conn = estado["conn"]
    execs = [e for e in conn.log if e[0] == "execute"]
    sqls = [e[1] for e in execs]
    i_del = next(i for i, s in enumerate(sqls) if s.startswith("DELETE"))
    i_merge = next(i for i, s in enumerate(sqls) if s.startswith("MERGE"))
    assert sqls[i_del] == "DELETE FROM [dbo].[t] WITH (HOLDLOCK) WHERE [op] = %s"
    assert execs[i_del][2] == (1,)  # value by bind, never in the text
    assert i_del < i_merge
    # Staging is already loaded BEFORE the DELETE: the scope lock lasts only DELETE + MERGE.
    i_carga = next(i for i, e in enumerate(conn.log) if e[0] == "executemany")
    assert i_carga < conn.log.index(execs[i_del])
    # A single commit, at the end -- none between the DELETE and the MERGE.
    assert conn.commits == 1 and conn.rollbacks == 0 and conn.log[-1] == ("commit",)


async def test_mssql_linha_fora_do_escopo_desfaz_sem_apagar(conexao_falsa):
    estado = conexao_falsa([1])  # 1 staging row does not belong to the scope
    with pytest.raises(ErroAplicacao) as exc:
        await _gravar_mssql()
    assert exc.value.codigo == "escrita_fora_do_escopo"
    conn = estado["conn"]
    assert not any(e[0] == "execute" and e[1].startswith("DELETE") for e in conn.log)
    assert conn.commits == 0 and conn.rollbacks == 1


async def test_mssql_escopo_sem_valor_nem_conecta(conexao_falsa):
    estado = conexao_falsa([])
    with pytest.raises(ErroAplicacao) as exc:
        await _gravar_mssql(params={"operacao_id": None})
    assert exc.value.codigo == "escrita_escopo_sem_valor"
    assert (
        estado["autocommit"] == []
    )  # defense in the connector itself: it did not even open a connection


async def test_mssql_contagem_le_a_linha_dict_do_pytds(conexao_falsa):
    """Regression: with `as_dict=True`, `SELECT @@ROWCOUNT` returns `{"0": n}`, and
    `cur.fetchone()[0]` raised KeyError -- which the generic `except` turned into `escrita_falhou`
    with a rollback. Every SQL Server write failed after writing."""
    conexao_falsa([2])
    n = await mssql.ConectorSqlServer().escrever_tabela(
        {},
        None,
        schema="dbo",
        tabela="t",
        colunas_destino=["op", "n"],
        linhas=[{"op": 1, "n": 1}, {"op": 1, "n": 2}],
        estrategia="append",
        chave=None,
        lote_tamanho=1000,
    )
    assert n == 2
    conexao_falsa([3])
    assert await mssql.ConectorSqlServer().executar_dml({}, None, "DELETE FROM x", None, None) == 3
