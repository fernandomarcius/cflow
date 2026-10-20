"""`limpar_escopo` against the compose Postgres: target `escrita_teste.alvo_escopo` in
`cflow_test`, through a connection pointing at the same database as owner (modeled on
`test_no_escrita_sql.py`).

The defect: clearing the scope in one node and writing in another are TWO transactions. A re-run
"from" the write node skips the cleanup (it sits upstream, copied as reused) and the MERGE rewrites
N1 rows over N2 > N1, so rows N1+1..N2 of the next run stay in the scope, silently adding up. Two
runs of the same scope interleaving DELETE/MERGE end up in the same place.

With `limpar_escopo`, the scope DELETE and the write are the same transaction (and, on Postgres,
the same per-scope lock), so rewriting REPLACES the scope, whatever the path.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy import text

from cflow_core.conexoes import service as cs
from cflow_core.conexoes.conectores import get_conector_escrita
from cflow_core.conexoes.parametros import NUMERADO, vincular
from cflow_core.errors import ErroAplicacao

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration

TABELA = "alvo_escopo"
ESCOPO = {"escopo": ":escopo_id"}


def _cfg_owner(owner_engine) -> tuple[dict, str]:
    u = owner_engine.url
    return (
        {"host": u.host, "porta": int(u.port), "base": u.database, "usuario": u.username},
        u.password,
    )


async def _preparar_alvo(owner_engine, seed=()) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS escrita_teste"))
        await conn.execute(text(f"DROP TABLE IF EXISTS escrita_teste.{TABELA}"))
        await conn.execute(
            text(
                f"CREATE TABLE escrita_teste.{TABELA} "
                "(escopo int NOT NULL, n int NOT NULL, c text, PRIMARY KEY (escopo, n))"
            )
        )
        for op, n, c in seed:
            await conn.execute(
                text(f"INSERT INTO escrita_teste.{TABELA} (escopo, n, c) VALUES (:escopo, :n, :c)"),
                {"escopo": op, "n": n, "c": c},
            )


async def _linhas(owner_engine, op: int) -> list[tuple]:
    async with owner_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    f"SELECT escopo, n, c FROM escrita_teste.{TABELA} WHERE escopo = :escopo ORDER BY n"
                ),
                {"escopo": op},
            )
        ).all()
    return [tuple(r) for r in rows]


async def _conexao_escrita(owner_engine, cliente, h, nome) -> str:
    config, senha = _cfg_owner(owner_engine)
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": nome, "tipo": "postgres", "config": config, "segredo": senha},
            headers=h,
        )
    ).json()["id"]
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
            {"i": cid},
        )
    return cid


async def _resolver(app_engine, cid: str):
    """The same gate as execution: `resolver_escrita` (credential + flag re-check)."""
    async with app_engine.begin() as conn:
        tipo, config, senha = await cs.resolver_escrita(conn, uuid.UUID(cid))
    return get_conector_escrita(tipo), config, senha


async def _gravar(conector, config, senha, op, linhas, *, estrategia="upsert"):
    return await conector.escrever_tabela_no_escopo(
        config,
        senha,
        schema="escrita_teste",
        tabela=TABELA,
        colunas_destino=["escopo", "n", "c"],
        linhas=linhas,
        estrategia=estrategia,
        chave=["escopo", "n"] if estrategia == "upsert" else None,
        lote_tamanho=1000,
        escopo=ESCOPO,
        params={"escopo_id": op},
    )


def _corrida(op: int, qtd: int, marca: str) -> list[dict]:
    return [{"escopo": op, "n": i, "c": marca} for i in range(1, qtd + 1)]


VIZINHO = [(2, 1, "nb"), (2, 2, "nb"), (2, 3, "nb")]


# ---------------- Connector layer ----------------


async def test_regravar_o_escopo_com_menos_linhas_nao_deixa_cauda(
    owner_engine, app_engine, cliente
):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc1@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=VIZINHO)
    conector, config, senha = await _resolver(
        app_engine, await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-1")
    )

    r1 = await _gravar(conector, config, senha, 1, _corrida(1, 5, "A"))
    assert r1 == {"linhas_afetadas": 5, "linhas_apagadas": 0}
    r2 = await _gravar(conector, config, senha, 1, _corrida(1, 2, "B"))
    # Only run 2 remains, entirely: no rows 3..5 of run 1.
    assert await _linhas(owner_engine, 1) == [(1, 1, "B"), (1, 2, "B")]
    assert r2 == {"linhas_afetadas": 2, "linhas_apagadas": 5}
    # The neighbouring scope is untouched.
    assert await _linhas(owner_engine, 2) == VIZINHO


async def test_append_com_escopo_substitui_em_vez_de_duplicar(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc2@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=VIZINHO)
    conector, config, senha = await _resolver(
        app_engine, await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-2")
    )
    # Without the DELETE in the same transaction, the 2nd append would violate the PK (1,1), or duplicate without it.
    for marca in ("A", "B"):
        await _gravar(conector, config, senha, 1, _corrida(1, 3, marca), estrategia="append")
    assert await _linhas(owner_engine, 1) == [(1, 1, "B"), (1, 2, "B"), (1, 3, "B")]
    assert await _linhas(owner_engine, 2) == VIZINHO


async def test_linha_fora_do_escopo_recusa_e_desfaz(owner_engine, app_engine, cliente):
    """A row written OUTSIDE the cleared scope is never deleted again by runs of this scope: it is
    a tail by construction. Refused, and the DELETE already done rolls back with it (same
    transaction)."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc3@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(1, 1, "orig"), (1, 2, "orig"), *VIZINHO])
    conector, config, senha = await _resolver(
        app_engine, await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-3")
    )
    with pytest.raises(ErroAplicacao) as exc:
        await _gravar(
            conector, config, senha, 1, [{"escopo": 1, "n": 1, "c": "x"}, *_corrida(9, 1, "x")]
        )
    assert exc.value.codigo == "escrita_fora_do_escopo"
    assert await _linhas(owner_engine, 1) == [(1, 1, "orig"), (1, 2, "orig")]
    assert await _linhas(owner_engine, 9) == []


async def test_escopo_sem_valor_recusa_sem_tocar_o_alvo(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc4@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(1, 1, "orig"), *VIZINHO])
    conector, config, senha = await _resolver(
        app_engine, await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-4")
    )
    with pytest.raises(ErroAplicacao) as exc:
        await _gravar(conector, config, senha, None, _corrida(1, 2, "x"))
    assert exc.value.codigo == "escrita_escopo_sem_valor"
    # Never `DELETE ... WHERE escopo = NULL` (deletes nothing and writes on top) nor a DELETE without WHERE.
    assert await _linhas(owner_engine, 1) == [(1, 1, "orig")]
    assert await _linhas(owner_engine, 2) == VIZINHO


async def test_trava_serializa_duas_corridas_do_mesmo_escopo(owner_engine, app_engine, cliente):
    """An outside transaction holds the lock of scope `escopo = '01'`: spelled differently from the
    `1` the run uses, and the SAME scope in the database. The run of scope 1 must wait; the run of
    scope 2 must not.

    This is what prevents the interleaving A.del, B.del, A.write(5), B.write(2), which left rows
    3..5 of A alongside those of B."""
    from cflow_core.conexoes.conectores.postgres import montar_sql_trava_escopo_pg

    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc5@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    conector, config, senha = await _resolver(
        app_engine, await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-5")
    )
    trava_sql, trava_vals = vincular(
        montar_sql_trava_escopo_pg("escrita_teste", TABELA, ESCOPO, {"escopo": "integer"}),
        {"escopo_id": "01"},
        NUMERADO,
    )
    u = owner_engine.url
    outra = await asyncpg.connect(
        host=u.host, port=u.port, user=u.username, password=u.password, database=u.database
    )
    try:
        tx = outra.transaction()
        await tx.start()
        await outra.execute(trava_sql, *trava_vals)

        corrida = asyncio.create_task(_gravar(conector, config, senha, 1, _corrida(1, 2, "B")))
        await asyncio.sleep(1.0)
        assert not corrida.done(), "the run of the locked scope did not wait for the lock"
        # A different scope does not contend for the lock.
        await asyncio.wait_for(_gravar(conector, config, senha, 2, _corrida(2, 1, "nb")), 10)
        assert await _linhas(owner_engine, 1) == []

        await tx.commit()  # release the lock
        await asyncio.wait_for(corrida, 10)
    finally:
        await outra.close()
    assert await _linhas(owner_engine, 1) == [(1, 1, "B"), (1, 2, "B")]
    assert await _linhas(owner_engine, 2) == [(2, 1, "nb")]


# ---------------- Pipeline: trigger and re-run through the API ----------------

PARAMETROS = [
    {"nome": "escopo_id", "tipo": "inteiro", "obrigatorio": True},
    {"nome": "qtd", "tipo": "inteiro", "obrigatorio": True},
]


def _definicao(cid: str, parametros=PARAMETROS) -> dict:
    return {
        "parametros": parametros,
        "nos": [
            {
                "id": "src",
                "tipo": "sql",
                "sql": (
                    "SELECT CAST(:escopo_id AS INTEGER) AS escopo, n, 'x' AS c "
                    "FROM (VALUES (1),(2),(3),(4),(5)) t(n) WHERE n <= :qtd"
                ),
            },
            {
                "id": "w",
                "tipo": "escrita_sql",
                "config": {
                    "conexao_id": cid,
                    "modo": "tabela",
                    "schema": "escrita_teste",
                    "tabela": TABELA,
                    "estrategia": "upsert",
                    "chave": ["escopo", "n"],
                    "limpar_escopo": ESCOPO,
                },
            },
        ],
        "arestas": [{"de": "src", "para": "w"}],
    }


async def _publicar(cliente, h, definicao: dict):
    pid = (
        await cliente.post("/pipelines", json={"nome": f"scope-{uuid.uuid4().hex[:6]}"}, headers=h)
    ).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid, r


async def _executar(cliente, h, pid, parametros) -> dict:
    r = await cliente.post(f"/pipelines/{pid}/executar", json={"parametros": parametros}, headers=h)
    assert r.status_code in (200, 201, 202), r.text
    return await aguardar_execucao(cliente, r.json()["id"], h)


async def test_reexecutar_a_partir_da_gravacao_nao_deixa_cauda(owner_engine, cliente):
    """The scenario end to end: A writes 2 rows; B, of the SAME scope, writes 5; someone re-runs A
    "from `w`". The upstream (`src`) is copied from A (2 rows) and only the write node runs;
    before, without the cleanup (which lived in another node, upstream), rows 3..5 of B remained."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc6@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=VIZINHO)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-6")
    pid, r = await _publicar(cliente, h, _definicao(cid))
    assert r.status_code == 201, r.text

    a = await _executar(cliente, h, pid, {"escopo_id": 1, "qtd": 2})
    assert a["status"] == "concluida", a
    b = await _executar(cliente, h, pid, {"escopo_id": 1, "qtd": 5})
    assert b["status"] == "concluida", b
    assert [n for _, n, _ in await _linhas(owner_engine, 1)] == [1, 2, 3, 4, 5]

    r = await cliente.post(f"/execucoes/{a['id']}/reexecutar", json={"desde_no": "w"}, headers=h)
    assert r.status_code == 200, r.text
    nova = await aguardar_execucao(cliente, r.json()["id"], h)
    assert nova["status"] == "concluida", nova
    nos = {n["no_id"]: n for n in nova["nos"]}
    assert nos["src"]["reaproveitado"] is True  # the cleanup must NOT depend on the upstream

    assert [n for _, n, _ in await _linhas(owner_engine, 1)] == [1, 2]
    assert await _linhas(owner_engine, 2) == VIZINHO


async def test_parametro_de_escopo_opcional_recusa_publicar_e_ausente_recusa_disparo(
    owner_engine, cliente
):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_esc7@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(1, 1, "orig")])
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-escopo-7")

    # Scope by an OPTIONAL parameter: left blank it would nullify the DELETE and the write would go
    # on top. Refused already when saving the draft; structural validation runs before publishing.
    opcional = [{"nome": "escopo_id", "tipo": "inteiro"}, PARAMETROS[1]]
    pid_op = (await cliente.post("/pipelines", json={"nome": "optional-scope"}, headers=h)).json()[
        "id"
    ]
    r = await cliente.put(
        f"/pipelines/{pid_op}/rascunho",
        json={"definicao": _definicao(cid, opcional), "base_numero": 0},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert "limpar_escopo" in r.text

    # Required and missing at trigger time: loud error before any run.
    pid, r = await _publicar(cliente, h, _definicao(cid))
    assert r.status_code == 201, r.text
    r = await cliente.post(f"/pipelines/{pid}/executar", json={"parametros": {"qtd": 2}}, headers=h)
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "parametros_invalidos"
    assert await _linhas(owner_engine, 1) == [(1, 1, "orig")]
