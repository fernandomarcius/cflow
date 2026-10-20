"""Two pieces of reuse in the engine: the copy of the reused snapshot (now a hardlink) and the
identity of a chain extraction (fingerprint with the parameters it depends on)."""

from __future__ import annotations

import errno
import os

from cflow_core.execucao import engine


def test_reuso_liga_o_mesmo_arquivo_em_vez_de_copiar(tmp_path):
    origem = tmp_path / "exec_a" / "n.parquet"
    origem.parent.mkdir()
    origem.write_bytes(b"PARQUET-A")
    destino = tmp_path / "exec_b" / "n.parquet"
    destino.parent.mkdir()

    engine._copiar_atomico(str(origem), destino)

    assert destino.read_bytes() == b"PARQUET-A"
    assert os.stat(destino).st_ino == os.stat(origem).st_ino, "copied instead of linking"
    assert not list(destino.parent.glob(".*.tmp")), "temporary file left behind"


def test_regravar_um_lado_nunca_altera_o_outro(tmp_path):
    # What makes the link safe: every snapshot writer writes to a temporary file and swaps it with
    # `os.replace` (new inode). If anyone started writing IN PLACE, both executions would share
    # the same data -- this test is the alarm.
    origem = tmp_path / "a.parquet"
    origem.write_bytes(b"ORIGINAL")
    destino = tmp_path / "b.parquet"
    engine._copiar_atomico(str(origem), destino)

    tmp = engine._tmp_ao_lado(destino)
    tmp.write_bytes(b"NEW")
    os.replace(tmp, destino)

    assert origem.read_bytes() == b"ORIGINAL"
    assert destino.read_bytes() == b"NEW"


def test_sem_link_possivel_copia_como_antes(tmp_path, monkeypatch):
    origem = tmp_path / "a.parquet"
    origem.write_bytes(b"PARQUET")
    destino = tmp_path / "b.parquet"

    def _sem_link(*_a, **_k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(engine.os, "link", _sem_link)
    engine._copiar_atomico(str(origem), destino)
    assert destino.read_bytes() == b"PARQUET"
    assert os.stat(destino).st_ino != os.stat(origem).st_ino


FONTE = {
    "id": "cfg_unit_item",
    "tipo": "fonte",
    "config": {
        "conexao_id": "c",
        "sql": "SELECT * FROM UnitItem_fn_Hist(:data_registro) WHERE unitId = :operacao_id",
    },
}


def test_extracao_sql_depende_so_dos_parametros_que_o_sql_cita():
    # Pipeline A does not declare `competencia`; pipeline B does. Same SQL, same value for the
    # cited ones: it is the SAME extraction (it used to be two fingerprints).
    from_a = {"operacao_id": "11", "data_registro": "2026-09-10 23:59:59"}
    from_b = {**from_a, "competencia_inicio": "2026-08-01", "competencia_fim": "2026-09-01"}
    p_a = engine._params_da_extracao(FONTE, from_a, from_a, por_referencia=True)
    p_b = engine._params_da_extracao(FONTE, from_b, from_b, por_referencia=True)
    assert p_a == p_b == from_a
    assert engine._fingerprint(FONTE, [], [], p_a) == engine._fingerprint(FONTE, [], [], p_b)


def test_extracao_sql_muda_quando_o_valor_citado_muda_ou_fica_em_branco():
    base = {"operacao_id": "11", "data_registro": "2026-09-10 23:59:59"}
    fp = engine._fingerprint(
        FONTE, [], [], engine._params_da_extracao(FONTE, base, base, por_referencia=True)
    )
    outra_op = {**base, "operacao_id": "12"}
    em_branco = {**base, "operacao_id": None}  # "blank = everything": predicate neutralized
    for p in (outra_op, em_branco):
        assert fp != engine._fingerprint(
            FONTE, [], [], engine._params_da_extracao(FONTE, p, p, por_referencia=True)
        )


def test_conector_que_nao_vincula_por_referencia_depende_de_todos():
    # `api_rest` sends EVERY runtime parameter as a query string: two pipelines with different
    # parameters request different URLs, even with the same `sql` (which does not apply there).
    a = {"operacao_id": "11"}
    b = {"operacao_id": "11", "competencia_inicio": "2026-08-01"}
    pa = engine._params_da_extracao(FONTE, a, a, por_referencia=False)
    pb = engine._params_da_extracao(FONTE, b, b, por_referencia=False)
    assert engine._fingerprint(FONTE, [], [], pa) != engine._fingerprint(FONTE, [], [], pb)
