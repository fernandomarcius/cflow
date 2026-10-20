"""`GET /publicados/{camada}/{dataset}/arquivo` - the parquet of ONE partition, byte for byte.

Clients stop paging `/dados` for bulk datasets (each page re-reads and SORTS the whole set) and
download the published file. Resolution and authorisation are those of `/dados`; what changes is the
response: the object bytes, streamed, with `Content-Length`, `X-Cflow-Linhas` and `X-Cflow-Sha256`
for the client to check.

Lake under `tmp` (CFLOW_LAKE_DIR), the same arrangement as `test_publicados.py`.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid

import pyarrow.parquet as pq
from sqlalchemy import text

from cflow_api.routes import publicados as rota

from ._arnes_auth import usuario_logado
from .test_publicados import (
    _criar_publicado,
    _def_projects,
    _def_sem_particao,
    _executar,
    _lake,
    _limpar_settings,
)
from .test_publicados_servico import _cenario, _conceder


def _arquivo(dataset: str, camada: str = "gold") -> str:
    sufixo = "?ambiente=dev" if camada == "gold" else ""
    return f"/publicados/{camada}/{dataset}/arquivo{sufixo}"


def _parquet_publicado(lake, camada: str, dataset: str, particao: dict) -> tuple[bytes, dict]:
    """The bytes the run wrote to the lake, found through the dataset `_atual.json` (the index of the
    current run) - what the route must return without rewriting."""
    raiz_dataset = lake / camada / "dev" / dataset if camada == "gold" else lake / camada / dataset
    indice = json.loads((raiz_dataset / "_atual.json").read_text())
    for p in indice.get("particoes") or [indice]:
        if particao.items() <= (p.get("particao") or {}).items():
            return (lake / camada / p["caminho"]).read_bytes(), p
    raise AssertionError(f"partition {particao} missing from the index")


async def _auditoria(owner_engine) -> int:
    async with owner_engine.connect() as c:
        return (await c.execute(text("SELECT count(*) FROM auditoria"))).scalar_one()


async def test_bytes_identicos_ao_parquet_publicado_e_cabecalhos(
    owner_engine, cliente, tmp_path, monkeypatch
):
    lake = _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq1@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_projects("arq_a"), "Projects file")
        ex = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "A"})
        esperado, entrada = _parquet_publicado(lake, "gold", "arq_a", {"project_id": "1201"})
        antes = await _auditoria(owner_engine)

        r = await cliente.get(
            _arquivo("arq_a"),
            params={"execucao_id": ex, "operacao_id": "61", "project_id": "1201"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.content == esperado, "the file goes out as it was published, without rewriting"
        assert r.headers["content-type"] == "application/vnd.apache.parquet"
        assert r.headers["content-length"] == str(len(esperado)) == str(entrada["bytes"])
        assert r.headers["x-cflow-sha256"] == hashlib.sha256(esperado).hexdigest()
        assert r.headers["x-cflow-linhas"] == "2" == str(entrada["linhas"])
        assert f"-{ex}.parquet" in r.headers["content-disposition"]
        # What the client checks: the parquet metadata matches X-Cflow-Linhas, in native types.
        meta = pq.ParquetFile(io.BytesIO(r.content))
        assert meta.metadata.num_rows == int(r.headers["x-cflow-linhas"])
        assert set(meta.schema_arrow.names) == {"PROJECTID", "valor", "lote"}
        assert await _auditoria(owner_engine) == antes, "a read leaves no audit entry"
    finally:
        _limpar_settings()


async def test_selecao_com_varios_arquivos_e_409_e_ausente_e_404(
    owner_engine, cliente, tmp_path, monkeypatch
):
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq2@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_projects("arq_b"), "Projects file b")
        ex = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "A"})

        r = await cliente.get(
            _arquivo("arq_b"), params={"execucao_id": ex, "operacao_id": "61"}, headers=h
        )
        assert r.status_code == 409, r.text
        erro = r.json()["erro"]
        assert erro["codigo"] == "publicado_varios_arquivos"
        assert "2 published files" in erro["mensagem"] and "project_id" in erro["mensagem"]

        for params, detalhe in (
            ({"project_id": "9999"}, "project_id=9999"),
            ({"operacao_id": "62", "project_id": "1201"}, "operacao_id=61"),
        ):
            r = await cliente.get(
                _arquivo("arq_b"), params={"execucao_id": ex, **params}, headers=h
            )
            assert r.status_code == 404, r.text
            assert r.json()["erro"]["codigo"] == "publicado_ausente"
            assert detalhe in r.json()["erro"]["mensagem"]

        r = await cliente.get(
            _arquivo("not_published"), params={"execucao_id": ex, "project_id": "1201"}, headers=h
        )
        assert r.status_code == 404 and r.json()["erro"]["codigo"] == "publicado_ausente"
    finally:
        _limpar_settings()


async def test_dataset_sem_particao_e_um_arquivo_sem_filtro(
    owner_engine, cliente, tmp_path, monkeypatch
):
    lake = _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq3@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_sem_particao("arq_sp"), "No partition")
        ex = await _executar(cliente, h, pid)
        esperado, _ = _parquet_publicado(lake, "silver", "arq_sp", {})

        r = await cliente.get(_arquivo("arq_sp", "silver"), params={"execucao_id": ex}, headers=h)
        assert r.status_code == 200, r.text
        assert r.content == esperado
        assert r.headers["x-cflow-linhas"] == "25"
        tipos = {f.name: str(f.type) for f in pq.ParquetFile(io.BytesIO(r.content)).schema_arrow}
        assert tipos == {"n": "int32", "v": "double", "d": "decimal128(18, 2)"}, "native types"
    finally:
        _limpar_settings()


async def test_resposta_e_streaming_em_blocos(owner_engine, cliente, tmp_path, monkeypatch):
    """The file does not pass whole through the API memory: the route returns the bytes in blocks of
    `BLOCO_ARQUIVO`. The test ASGI client joins the body, so the proof is at the stream source."""
    lake = _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq4@example.com", perfil="especialista"
        )
        definicao = _def_sem_particao("arq_grande")
        definicao["nos"][0]["sql"] = (
            "SELECT range AS n, md5(CAST(range AS VARCHAR)) AS h FROM range(50000)"
        )
        pid = await _criar_publicado(cliente, h, definicao, "Large")
        ex = await _executar(cliente, h, pid)
        esperado, _ = _parquet_publicado(lake, "silver", "arq_grande", {})

        monkeypatch.setattr(rota.publicados, "BLOCO_ARQUIVO", 64 * 1024)
        blocos: list[int] = []
        original = rota.publicados.em_blocos

        async def _espiao(arq, bloco=None):
            async for b in original(arq, bloco):
                blocos.append(len(b))
                yield b

        monkeypatch.setattr(rota.publicados, "em_blocos", _espiao)
        r = await cliente.get(
            _arquivo("arq_grande", "silver"), params={"execucao_id": ex}, headers=h
        )
        assert r.status_code == 200, r.text
        assert r.content == esperado
        assert len(esperado) > 4 * 64 * 1024, "precondition: a file larger than several blocks"
        assert len(blocos) > 4 and max(blocos) <= 64 * 1024, blocos
        assert sum(blocos) == len(esperado)
    finally:
        _limpar_settings()


async def test_arquivo_adulterado_no_lake_e_502(owner_engine, cliente, tmp_path, monkeypatch):
    lake = _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq5@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_sem_particao("arq_adult"), "Tampered")
        ex = await _executar(cliente, h, pid)
        _, entrada = _parquet_publicado(lake, "silver", "arq_adult", {})
        with open(lake / "silver" / entrada["caminho"], "ab") as f:
            f.write(b"junk")

        r = await cliente.get(
            _arquivo("arq_adult", "silver"), params={"execucao_id": ex}, headers=h
        )
        assert r.status_code == 502, r.text
        assert r.json()["erro"]["codigo"] == "publicado_divergente"
    finally:
        _limpar_settings()


async def test_operador_nao_le(owner_engine, cliente, tmp_path, monkeypatch):
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq6@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_sem_particao("arq_op"), "Operator")
        ex = await _executar(cliente, h, pid)
        _, ho = await usuario_logado(
            owner_engine, cliente, email="arq6op@example.com", perfil="operador"
        )
        r = await cliente.get(_arquivo("arq_op", "silver"), params={"execucao_id": ex}, headers=ho)
        assert r.status_code == 403, r.text
    finally:
        _limpar_settings()


async def test_parametros_de_dados_sao_recusados_antes_do_lake(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """`filtro`/`take`/`skip`/`ordenar`/`agregar` belong to `/dados`. On `/arquivo` they would have no
    effect - and silently accepted, `filtro=project=X` would return the whole file dressed as a
    slice. The refusal comes before resolution: neither the run database nor the lake is consulted."""
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="arq7@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_projects("arq_par"), "Data parameters")
        ex = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "A"})
        base = {"execucao_id": ex, "operacao_id": "61", "project_id": "1201"}
        assert (await cliente.get(_arquivo("arq_par"), params=base, headers=h)).status_code == 200
        # The defect this refusal closes: the requested slice does not exist, and without it a 200
        # came with the whole file.
        r = await cliente.get(
            _arquivo("arq_par"), params={**base, "filtro": "PROJECTID=does_not_exist"}, headers=h
        )
        assert r.status_code == 400, (r.status_code, r.headers.get("content-type"))

        async def _nao_resolve(*_a, **_k):
            raise AssertionError("the refusal must come before resolving the publication")

        monkeypatch.setattr(rota, "_publicacao", _nao_resolve)
        casos = (
            {"filtro": "PROJECTID=does_not_exist"},
            {"take": "1"},
            {"skip": "0"},
            {"ordenar": "-valor"},
            {"agregar": "sum:valor"},
            {"take": ""},
        )
        for extra in casos:
            r = await cliente.get(_arquivo("arq_par"), params={**base, **extra}, headers=h)
            assert r.status_code == 400, (extra, r.status_code, r.text[:200])
            erro = r.json()["erro"]
            assert erro["codigo"] == "parametro_nao_e_do_arquivo"
            (nome,) = extra
            assert nome in erro["mensagem"] and "/dados" in erro["mensagem"]
            assert [d["campo"] for d in erro["detalhes"]] == [nome]

        r = await cliente.get(
            _arquivo("arq_par"),
            params={**base, "filtro": "x=1", "take": "1", "ordenar": "-x"},
            headers=h,
        )
        assert r.status_code == 400
        assert [d["campo"] for d in r.json()["erro"]["detalhes"]] == ["take", "filtro", "ordenar"]

        # Nonexistent run: still a 400, not a 404 - proof that nothing was resolved.
        r = await cliente.get(
            _arquivo("arq_par"),
            params={"execucao_id": str(uuid.uuid4()), "filtro": "x=1"},
            headers=h,
        )
        assert r.status_code == 400 and r.json()["erro"]["codigo"] == "parametro_nao_e_do_arquivo"
    finally:
        _limpar_settings()


# --- SERVICE account: the same rules as `/dados` ------------------------------------------------


async def test_servico_com_ver_baixa_os_mesmos_bytes(owner_engine, cliente, tmp_path, monkeypatch):
    try:
        admin, h, pid, ex, uid, _, hs = await _cenario(
            owner_engine, cliente, tmp_path, monkeypatch, "arqver"
        )
        await _conceder(uid, pid, admin)
        params = {"execucao_id": ex, "operacao_id": "61", "project_id": "1202"}
        r = await cliente.get(_arquivo("svc_arqver"), params=params, headers=hs)
        assert r.status_code == 200, r.text
        r_admin = await cliente.get(_arquivo("svc_arqver"), params=params, headers=h)
        assert r.content == r_admin.content
        assert r.headers["x-cflow-sha256"] == hashlib.sha256(r.content).hexdigest()
        assert r.headers["x-cflow-linhas"] == "1"
    finally:
        _limpar_settings()


async def test_servico_baixa_sem_concessao_e_inexistente_da_403(
    owner_engine, cliente, tmp_path, monkeypatch
):
    try:
        _, _, _, ex, _, _, hs = await _cenario(
            owner_engine, cliente, tmp_path, monkeypatch, "arqsem"
        )
        # Without a grant: the service account reaches every pipeline.
        sem = await cliente.get(
            _arquivo("svc_arqsem"), params={"execucao_id": ex, "project_id": "1201"}, headers=hs
        )
        assert sem.status_code == 200, sem.text
        inexistente = await cliente.get(
            _arquivo("svc_arqsem"),
            params={"execucao_id": str(uuid.uuid4()), "project_id": "1201"},
            headers=hs,
        )
        assert inexistente.status_code == 403, inexistente.text
        assert inexistente.json()["erro"]["codigo"] == "acesso_negado"
    finally:
        _limpar_settings()


async def test_pipeline_sistema_aberto_e_token_revogado_401(
    owner_engine, cliente, tmp_path, monkeypatch
):
    try:
        admin, h, pid, ex, uid, token_id, hs = await _cenario(
            owner_engine, cliente, tmp_path, monkeypatch, "arqsis"
        )
        await _conceder(uid, pid, admin)
        params = {"execucao_id": ex, "project_id": "1201"}
        assert (
            await cliente.get(_arquivo("svc_arqsis"), params=params, headers=hs)
        ).status_code == 200

        # `sistema` does not close for the service account.
        r = await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=h)
        assert r.status_code == 200, r.text
        r = await cliente.get(_arquivo("svc_arqsis"), params=params, headers=hs)
        assert r.status_code == 200, r.text

        assert (
            await cliente.delete(f"/usuarios/{uid}/tokens/{token_id}", headers=h)
        ).status_code == 204
        r = await cliente.get(_arquivo("svc_arqsis"), params=params, headers=hs)
        assert r.status_code == 401, r.text
    finally:
        _limpar_settings()
