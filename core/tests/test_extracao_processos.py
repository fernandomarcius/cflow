"""Source extraction in PROCESSES.

These tests protect the BOUNDARY: when slicing is safe, which SQL a slice becomes, and that a
wrong config is rejected instead of silently becoming a sequential extraction. The gain itself
is measured on a real deployment; tests do not measure performance.
"""

import pytest

from cflow_core.conexoes import processos
from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.definicao import validar

SQL = "SELECT a, b FROM T (nolock) WHERE x = 1"


class TestParticionavel:
    def test_select_simples_pode_ser_fatiado(self):
        assert processos.particionavel(SQL)

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH c AS (SELECT 1 AS a) SELECT * FROM c",  # wrapping a CTE is invalid T-SQL
            "SELECT TOP 10 a FROM T",  # limiting inside the slice does not limit the whole
            "SELECT a FROM T ORDER BY a",  # ordering inside the slice does not order the whole
            "SELECT a FROM T OFFSET 10 ROWS",
            "SELECT a FROM _entrada",  # session staging does not exist in the child process
            "SELECT a FROM T FOR JSON PATH",
            "SELECT a FROM T OPTION (MAXDOP 1)",  # the slice adds its own OPTION
        ],
    )
    def test_recusa_o_que_o_embrulho_mudaria(self, sql):
        assert not processos.particionavel(sql)

    def test_palavra_proibida_dentro_de_comentario_nao_bloqueia(self):
        assert processos.particionavel("SELECT a FROM T -- ORDER BY a\n")


class TestSqlDaFatia:
    def test_does_not_use_percent__collides_with_pytds_interpolation(self):
        """A literal `%` becomes a format specifier in pytds ("not enough arguments for format
        string"), the same collision as LIKE. The remainder comes from `c - (c / n) * n`."""
        sql = processos.sql_da_fatia(SQL, "Id", 4, 2)
        assert "%" not in sql
        assert "ABS(CHECKSUM(_fatia.[Id]) - (CHECKSUM(_fatia.[Id]) / 4) * 4) = 2" in sql

    def test_o_resto_vem_antes_do_abs(self):
        """`ABS(CHECKSUM(...))` overflows at -2,147,483,648 ("Arithmetic overflow"), and only on
        the row that produces it, i.e. in production. Taking the remainder first keeps the value
        within -(n-1)..(n-1)."""
        sql = processos.sql_da_fatia(SQL, "Id", 4, 2)
        assert sql.index("ABS(") < sql.index(" / 4")

    def test_as_fatias_sao_disjuntas_e_exaustivas_por_construcao(self):
        """Each slice tests the SAME predicate against a different value: every row falls into
        exactly one bucket, whatever the CHECKSUM."""
        # `rsplit`: the user SQL also has a WHERE; the slice predicate is the LAST one.
        pred = [processos.sql_da_fatia(SQL, "Id", 3, k).rsplit("WHERE", 1)[1] for k in range(3)]
        assert sorted(pred) == sorted(set(pred))
        assert all(
            "ABS(CHECKSUM(_fatia.[Id]) - (CHECKSUM(_fatia.[Id]) / 3) * 3)" in p for p in pred
        )

    def test_coluna_que_nao_e_identificador_e_recusada(self):
        with pytest.raises(processos.ForaDoParalelo):
            processos.sql_da_fatia(SQL, "Id; DROP TABLE T", 2, 0)


class TestPlano:
    def test_sem_config_nao_fatia(self):
        assert processos.ler_plano(None, 4) is None

    def test_processos_limitado_pelo_teto_do_worker(self):
        assert processos.ler_plano({"coluna": "Id", "processos": 32}, 4).processos == 4

    def test_sem_processos_usa_o_teto(self):
        assert processos.ler_plano({"coluna": "Id"}, 3).processos == 3

    @pytest.mark.parametrize(
        "cfg",
        [{"processos": 4}, {"coluna": "a b"}, {"coluna": "Id", "processos": 1}],
    )
    def test_config_malformada_levanta_em_vez_de_rodar_sequencial(self, cfg):
        with pytest.raises(processos.ForaDoParalelo):
            processos.ler_plano(cfg, 4)


def _erros(extracao_paralela) -> list[str]:
    """Rejection messages from validation (empty list = passed)."""
    try:
        validar(_definicao(extracao_paralela))
    except ErroAplicacao as exc:
        return [d.mensagem if hasattr(d, "mensagem") else d["mensagem"] for d in exc.detalhes]
    return []


def _definicao(extracao_paralela):
    return {
        "nos": [
            {
                "id": "f",
                "tipo": "fonte",
                "config": {
                    "conexao_id": "11111111-1111-1111-1111-111111111111",
                    "sql": SQL,
                    "extracao_paralela": extracao_paralela,
                },
            }
        ],
        "arestas": [],
    }


class TestValidacao:
    def test_config_boa_passa(self):
        assert _erros({"coluna": "OrderId", "processos": 4}) == []

    def test_campo_desconhecido_e_recusado(self):
        """Accepted silently, `colunas` (plural) would look configured and configure nothing."""
        assert any("colunas" in m for m in _erros({"coluna": "Id", "colunas": ["Id"]}))

    def test_sem_coluna_e_recusado(self):
        assert any("coluna" in m for m in _erros({"processos": 4}))

    def test_coluna_que_nao_e_identificador_e_recusada(self):
        assert any("extracao_paralela.coluna" in m for m in _erros({"coluna": "1; DROP TABLE T"}))


class TestTetoDeProcessos:
    """The cap follows the SAME order as `duck.orcamento`: cgroup quota before affinity."""

    def _teto(self, monkeypatch, *, quota, afinidade, configurado=4):
        from types import SimpleNamespace

        from cflow_core import duck
        from cflow_core.conexoes.conectores.sqlserver import _teto_de_processos

        monkeypatch.setattr(duck, "cpus_cgroup", lambda: quota)
        monkeypatch.setattr(duck, "cpus_afinidade", lambda: afinidade)
        return _teto_de_processos(SimpleNamespace(extracao_processos_max=configurado))

    def test_quota_do_cgroup_vence_a_afinidade(self, monkeypatch):
        """A pod limited to 2 CPUs on a 32-core node: affinity would say 32."""
        assert self._teto(monkeypatch, quota=2.0, afinidade=32) == 2

    def test_sem_quota_vale_a_afinidade(self, monkeypatch):
        assert self._teto(monkeypatch, quota=None, afinidade=3) == 3

    def test_quota_fracionaria_arredonda_para_baixo(self, monkeypatch):
        assert self._teto(monkeypatch, quota=2.5, afinidade=8) == 2

    def test_nunca_passa_do_configurado(self, monkeypatch):
        assert self._teto(monkeypatch, quota=None, afinidade=16, configurado=4) == 4

    def test_teto_abaixo_de_dois_nao_fatia(self):
        """A single slice is the sequential path plus the cost of the child and IPC."""
        assert processos.ler_plano({"coluna": "Id", "processos": 4}, 1) is None


class TestPoolUnico:
    """The pool is ONE per worker process. A version without a lock created two under concurrent
    extractions (8 children for a cap of 4) and hung: the orphaned pool held on to the children."""

    def _contador(self, monkeypatch):
        import threading
        import time

        criados = []

        class PoolFalso:
            def __init__(self, max_workers, mp_context):
                time.sleep(0.01)  # widens the race window, like a real spawn
                criados.append(max_workers)

            def shutdown(self, wait=True):
                pass

        monkeypatch.setattr(processos, "ProcessPoolExecutor", PoolFalso)
        monkeypatch.setattr(processos, "_POOL", None)
        monkeypatch.setattr(processos, "_TRAVA", threading.Lock())
        return criados

    def test_threads_simultaneas_criam_um_pool_so(self, monkeypatch):
        import threading

        criados = self._contador(monkeypatch)
        vistos, largada = [], threading.Barrier(16)

        def pedir():
            largada.wait()
            vistos.append(id(processos._pool(4)))

        ts = [threading.Thread(target=pedir) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert criados == [4]
        assert len(set(vistos)) == 1

    def test_pedido_de_outro_tamanho_nao_troca_o_pool(self, monkeypatch):
        """Swapping the pool has the same flaw: the old one may be in the middle of another thread's `map`."""
        criados = self._contador(monkeypatch)
        primeiro = processos._pool(2)
        assert processos._pool(4) is primeiro
        assert criados == [2]

    def test_plano_carrega_o_teto_do_pool(self):
        plano = processos.ler_plano({"coluna": "Id", "processos": 2}, 4)
        assert (plano.processos, plano.teto) == (2, 4)


def test_slice_recompiles__cached_plan_was_sniffed_by_another_query():
    """Each slice is a text with its own cached plan; without RECOMPILE, a plan compiled for a
    small query would be reused for one with far more reads (parameter sniffing)."""
    assert processos.sql_da_fatia(SQL, "Id", 4, 2).endswith("OPTION (RECOMPILE)")
