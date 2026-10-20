"""Per-process caches that take transactions out of the fixed per-node cost.

Each one answers the same question: how many database round trips remain, and what stays fresh
when the source of truth changes. No database: reads are replaced by counters."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from cflow_core.conexoes import service as conexoes
from cflow_core.execucao import engine, instancia
from cflow_core.ingestao import armazenamento


async def test_fatos_da_execucao_le_uma_vez_para_nos_simultaneos(monkeypatch):
    chamadas = []

    async def _ler(execucao_id, chave):
        chamadas.append(chave)
        await asyncio.sleep(0.01)  # the other nodes arrive while the read is in flight
        fatos = engine._FATOS_EXECUCAO[chave] = {"pipeline_id": "p", "cadeia_id": None}
        return fatos

    monkeypatch.setattr(engine, "_ler_fatos", _ler)
    eid = uuid.uuid4()
    res = await asyncio.gather(*(engine._fatos_da_execucao(eid) for _ in range(8)))
    assert len(chamadas) == 1
    assert all(r == {"pipeline_id": "p", "cadeia_id": None} for r in res)
    assert str(eid) not in engine._FATOS_EM_VOO  # the flight ends and leaves the map
    engine._FATOS_EXECUCAO.pop(str(eid), None)


async def test_fatos_da_execucao_falha_chega_a_todos_e_nao_fica_presa(monkeypatch):
    async def _ler(execucao_id, chave):
        await asyncio.sleep(0.01)
        raise RuntimeError("database down")

    monkeypatch.setattr(engine, "_ler_fatos", _ler)
    eid = uuid.uuid4()
    res = await asyncio.gather(
        *(engine._fatos_da_execucao(eid) for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(r, RuntimeError) for r in res)
    assert str(eid) not in engine._FATOS_EM_VOO  # the next attempt reads again


async def test_vincula_por_referencia_le_o_tipo_uma_vez(monkeypatch):
    lidas = []

    @contextlib.asynccontextmanager
    async def _transacao():
        yield None

    async def _carregar(conn, conexao_id):
        lidas.append(conexao_id)
        return SimpleNamespace(tipo="sqlserver")

    monkeypatch.setattr(conexoes, "transacao", _transacao)
    monkeypatch.setattr(conexoes, "_carregar", _carregar)
    cid = uuid.uuid4()
    assert await conexoes.vincula_por_referencia(cid) is True
    assert await conexoes.vincula_por_referencia(cid) is True
    assert len(lidas) == 1


async def test_vincula_por_referencia_conexao_inexistente_nao_entra_no_cache(monkeypatch):
    @contextlib.asynccontextmanager
    async def _transacao():
        yield None

    async def _carregar(conn, conexao_id):
        return None

    monkeypatch.setattr(conexoes, "transacao", _transacao)
    monkeypatch.setattr(conexoes, "_carregar", _carregar)
    cid = uuid.uuid4()
    assert await conexoes.vincula_por_referencia(cid) is False
    assert str(cid) not in conexoes._TIPO_DA_CONEXAO


def _acesso_local(raiz: str):
    return SimpleNamespace(tipo="objeto", config={"provedor": "local", "raiz": raiz}, senha=None)


async def test_abrir_reusa_o_backend_ate_ser_esquecido(monkeypatch, tmp_path):
    resolvidos = []

    async def _obter_acesso(conexao_id):
        resolvidos.append(conexao_id)
        return _acesso_local(str(tmp_path / str(len(resolvidos))))

    monkeypatch.setattr(conexoes, "obter_acesso", _obter_acesso)
    cid = uuid.uuid4()
    a = await armazenamento.abrir(cid)
    b = await armazenamento.abrir(str(cid))
    assert a is b and len(resolvidos) == 1
    # `editar`/`rotacionar_credencial`/`excluir` forget: the new config applies on the next open.
    armazenamento.esquecer(cid)
    c = await armazenamento.abrir(cid)
    assert len(resolvidos) == 2 and c.raiz != a.raiz
    armazenamento.esquecer(cid)


async def test_abrir_vence_pelo_ttl(monkeypatch, tmp_path):
    resolvidos = []

    async def _obter_acesso(conexao_id):
        resolvidos.append(conexao_id)
        return _acesso_local(str(tmp_path))

    relogio = [1000.0]
    monkeypatch.setattr(conexoes, "obter_acesso", _obter_acesso)
    monkeypatch.setattr(armazenamento.time, "monotonic", lambda: relogio[0])
    cid = uuid.uuid4()
    await armazenamento.abrir(cid)
    relogio[0] += armazenamento._TTL_BACKEND_S - 1
    await armazenamento.abrir(cid)
    assert len(resolvidos) == 1
    relogio[0] += 2  # another process (api) may have swapped the credential: the TTL is the ceiling
    await armazenamento.abrir(cid)
    assert len(resolvidos) == 2
    armazenamento.esquecer(cid)


async def test_abrir_sem_conexao_continua_local_sem_cache(monkeypatch, tmp_path):
    async def _proibido(conexao_id):  # air-gapped: without a connection there is no vault to query
        raise AssertionError("must not resolve a credential")

    monkeypatch.setattr(conexoes, "obter_acesso", _proibido)
    arm = await armazenamento.abrir(None)
    assert isinstance(arm, armazenamento.ArmazenamentoLocal)


def test_editar_esquece_o_backend_do_lake():
    # The invalidation point is the helper that `editar`, `rotacionar_credencial` and `excluir` call.
    cid = uuid.uuid4()
    armazenamento._BACKENDS[str(cid)] = (0.0, armazenamento.ArmazenamentoLocal(raiz=None))
    conexoes._esquecer_backend(cid)
    assert str(cid) not in armazenamento._BACKENDS


# --- Hash and chain extraction memo --------------------------------------------------------------
#
# Both caches save one database round trip per node. What makes them safe is not the TTL but the
# file IDENTITY: the stored value describes a snapshot, and whoever rebuilds a snapshot writes to
# a temporary file and swaps it with `os.replace` (new inode). If another replica rebuilt the
# predecessor, the identity no longer matches and the value is read from the database again.


def _snap(tmp_path, nome: str, conteudo: bytes = b"a") -> Path:
    p = tmp_path / f"{nome}.parquet"
    p.write_bytes(conteudo)
    return p


def test_hash_lembrado_volta_o_que_foi_gravado(tmp_path):
    engine._HASH_NO.clear()
    ex, snap = uuid.uuid4(), _snap(tmp_path, "n1")
    engine._lembrar_no(ex, "n1", "10:7", snap)
    assert engine._hash_lembrado(ex, "n1", snap) == "10:7"


def test_hash_lembrado_cai_fora_quando_o_snapshot_e_refeito(tmp_path):
    """A retry on ANOTHER replica swaps the file (new inode). The stale hash would enter this
    node's fingerprint and cause a snapshot computed from a different input to be reused."""
    engine._HASH_NO.clear()
    ex, snap = uuid.uuid4(), _snap(tmp_path, "n1")
    engine._lembrar_no(ex, "n1", "10:7", snap)
    outro = _snap(tmp_path, "n1_nova", b"b")
    os.replace(outro, snap)  # this is what every snapshot writer does
    assert engine._hash_lembrado(ex, "n1", snap) is None


def test_hash_lembrado_sem_arquivo_e_none(tmp_path):
    engine._HASH_NO.clear()
    ex = uuid.uuid4()
    assert engine._hash_lembrado(ex, "n1", tmp_path / "nao_existe.parquet") is None


def test_cadeia_lembrada_cai_fora_quando_o_snapshot_some(tmp_path):
    engine._SNAP_DA_CADEIA.clear()
    chave = ("cad", "fp")
    snap = _snap(tmp_path, "fonte")
    engine._lembrar_da_cadeia(chave, str(snap), "3:1", 3)
    assert engine._lembrado_da_cadeia(chave)["linhas"] == 3
    snap.unlink()  # snapshot purge: the cache must not serve a path that no longer exists
    assert engine._lembrado_da_cadeia(chave) is None


async def test_trazer_snapshot_liga_no_loop_e_copia_quando_o_fs_recusa(tmp_path, monkeypatch):
    """The link is metadata (microseconds) and runs on the loop; the COPY is I/O and goes to a thread."""
    origem = _snap(tmp_path, "origem", b"dados")
    destino = tmp_path / "destino.parquet"
    await engine._trazer_snapshot(str(origem), destino)
    assert destino.read_bytes() == b"dados"
    assert os.stat(destino).st_ino == os.stat(origem).st_ino  # link, not copy

    destino2 = tmp_path / "destino2.parquet"
    monkeypatch.setattr(engine.os, "link", lambda *a: (_ for _ in ()).throw(OSError("EXDEV")))
    await engine._trazer_snapshot(str(origem), destino2)
    assert destino2.read_bytes() == b"dados"
    assert os.stat(destino2).st_ino != os.stat(origem).st_ino  # fell back to copy


def test_snapshot_e_hash_fecha_a_instancia_num_salto_so(tmp_path):
    """Outside the output, the same hop writes the snapshot, takes the hash and CLOSES: the thread
    owns the instance, so the `finally` must not close it again."""
    duck = duckdb.connect()
    duck.execute("CREATE TABLE n1 AS SELECT 1 AS a UNION ALL SELECT 2")
    snap = tmp_path / "n1.parquet"
    h = engine._snapshot_e_hash(duck, "n1", snap, None, instancia.Slot(duck, None))
    assert h.startswith("2:")  # the hash carries the count
    assert snap.exists()
    with pytest.raises(duckdb.Error):
        duck.execute("SELECT 1")


def test_snapshot_e_hash_de_repasse_nao_regrava_o_parquet(tmp_path):
    """Pure passthrough: the snapshot is a LINK to the source file (same inode), and the hash comes from the view."""
    origem = tmp_path / "origem.parquet"
    duck = duckdb.connect()
    duck.execute("CREATE TABLE fonte AS SELECT 1 AS a UNION ALL SELECT 2")
    duck.execute(f"COPY fonte TO '{origem}' (FORMAT parquet)")
    duck.execute(f"CREATE VIEW n1 AS SELECT * FROM read_parquet('{origem}')")
    snap = tmp_path / "n1.parquet"
    h = engine._snapshot_e_hash(duck, "n1", snap, origem, None)
    assert os.stat(snap).st_ino == os.stat(origem).st_ino
    assert h == engine._hash_conteudo(duck, "n1")
    duck.close()


def test_colunas_iguais_recusa_o_link_quando_o_caminho_hive_acrescenta_coluna(tmp_path):
    """The lake read adds the partition keys from the PATH. Passing the file through by link there
    would hand the consumer a snapshot with fewer columns than the node computed."""
    alvo = tmp_path / "ds" / "operacao_id=1" / "execucao_id=abc" / "dados.parquet"
    alvo.parent.mkdir(parents=True)
    duck = duckdb.connect()
    duck.execute("CREATE TABLE base AS SELECT 1 AS a, 'x' AS b")
    duck.execute(f"COPY base TO '{alvo}' (FORMAT PARQUET)")

    duck.execute(f"CREATE VIEW com_hive AS SELECT * FROM read_parquet(['{alvo}'])")
    assert [c[0] for c in duck.execute("DESCRIBE SELECT * FROM com_hive").fetchall()] == [
        "a",
        "b",
        "execucao_id",
        "operacao_id",
    ]
    assert engine._colunas_iguais(duck, "com_hive", str(alvo)) is False

    solto = tmp_path / "solto.parquet"
    duck.execute(f"COPY base TO '{solto}' (FORMAT PARQUET)")
    duck.execute(f"CREATE VIEW sem_hive AS SELECT * FROM read_parquet(['{solto}'])")
    assert engine._colunas_iguais(duck, "sem_hive", str(solto)) is True
    duck.close()


def test_hash_por_arquivo_e_o_mesmo_que_recalcular(tmp_path):
    """Two passthrough nodes of the SAME file build the same view over the same bytes: one's
    `to_json` is the other's. This differs from inheriting the hash from the PRODUCER (its
    in-memory table), which fails: here both sides are the same read of the same inode.

    It includes a HUGEINT on purpose: that type does not survive the Parquet round trip. Here it
    does not matter, because nobody reads the original table."""
    arq = tmp_path / "dados.parquet"
    d1 = duckdb.connect()
    d1.execute(
        "CREATE TABLE base AS SELECT n, c, sum(n) OVER () AS total "
        "FROM (VALUES (1,'a'),(2,'b'),(3,NULL)) t(n,c)"
    )
    d1.execute(f"COPY base TO '{arq}' (FORMAT PARQUET)")
    d1.execute(f"CREATE VIEW n1 AS SELECT * FROM read_parquet('{arq}')")
    primeiro = engine._hash_conteudo(d1, "n1")
    engine._lembrar_hash_do_arquivo(str(arq), primeiro)

    d2 = duckdb.connect()
    d2.execute(f"CREATE VIEW n2 AS SELECT * FROM read_parquet('{arq}')")
    assert engine._hash_do_arquivo(str(arq)) == primeiro
    assert engine._hash_conteudo(d2, "n2") == primeiro, "the memo must equal the recomputed value"
    d1.close()
    d2.close()


def test_hash_por_arquivo_nao_vale_para_outro_arquivo_nem_apos_reescrita(tmp_path):
    """The key is (device, inode, size, mtime). A file swapped underneath, which happens when
    another replica rebuilds the predecessor, returns None and the hash is computed again."""
    arq = tmp_path / "d.parquet"
    duck = duckdb.connect()
    duck.execute("CREATE TABLE a AS SELECT 1 AS x")
    duck.execute(f"COPY a TO '{arq}' (FORMAT PARQUET)")
    engine._lembrar_hash_do_arquivo(str(arq), "1:111")
    assert engine._hash_do_arquivo(str(arq)) == "1:111"

    duck.execute("CREATE TABLE b AS SELECT 1 AS x, 2 AS y")
    duck.execute(f"COPY b TO '{arq}' (FORMAT PARQUET)")  # same path, different bytes
    assert engine._hash_do_arquivo(str(arq)) is None
    assert engine._hash_do_arquivo(str(tmp_path / "nao-existe.parquet")) is None
    duck.close()
