"""Streaming upload of the GCS `gravar_arquivo`: the object goes from disk to the network in
blocks, without the whole file in memory, with the same md5, the same size and the same retry.

The simulated server CONSUMES the body without keeping it (counts bytes and md5), so that the peak
measured by `tracemalloc` is the client's - an `httpx.MockTransport` would read the whole body with
`aread()` and measure the server itself."""

import base64
import hashlib
import json
import os
import tracemalloc

import httpx
import pytest

from cflow_core.ingestao import armazenamento as A


@pytest.fixture(autouse=True)
def _gcs_permitido(monkeypatch):
    monkeypatch.setenv("CFLOW_LAKE_SOMENTE_LOCAL", "false")
    from cflow_core.config import get_settings

    get_settings.cache_clear()
    A.limpar_cache_token()
    yield
    A.limpar_cache_token()
    get_settings.cache_clear()


def _credencial() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = chave.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "client_email": "ingestion@example-project.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }


class _Servidor(httpx.AsyncBaseTransport):
    """Answers the token and consumes each upload in streaming, returning `md5Hash`/`size` of what
    it received. `falhas` = how many uploads answer 503 before accepting."""

    def __init__(self, falhas: int = 0):
        self.falhas = falhas
        self.uploads: list[dict] = []

    async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        h, n = hashlib.md5(usedforsecurity=False), 0
        async for bloco in req.stream:
            h.update(bloco)
            n += len(bloco)
        md5 = base64.b64encode(h.digest()).decode()
        self.uploads.append({"bytes": n, "md5": md5, "headers": dict(req.headers)})
        if self.falhas:
            self.falhas -= 1
            return httpx.Response(503, json={"error": "backend"})
        return httpx.Response(200, json={"name": "o", "md5Hash": md5, "size": str(n)})


def _lake(monkeypatch, servidor: _Servidor) -> A.ArmazenamentoGcs:
    original = httpx.AsyncClient

    def _fabrica(*args, **kwargs):
        kwargs["transport"] = servidor
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fabrica)
    config = {
        "provedor": "gcs",
        "buckets": {"landing": "l", "bronze": "b", "quarantine": "q", "gold": "g"},
    }
    return A.montar("objeto", config, json.dumps(_credencial()))


def _arquivo(tmp_path, mib: int):
    p = tmp_path / "dados.parquet"
    with p.open("wb") as f:
        for _ in range(mib):
            f.write(os.urandom(1 << 20))
    h = hashlib.md5(p.read_bytes(), usedforsecurity=False).digest()
    return p, base64.b64encode(h).decode()


async def test_arquivo_grande_vai_em_streaming_com_o_mesmo_md5_e_tamanho(tmp_path, monkeypatch):
    p, md5 = _arquivo(tmp_path, 3)
    srv = _Servidor()
    uri = await _lake(monkeypatch, srv).gravar_arquivo("gold", "ds/dados.parquet", p)
    assert uri == "gs://g/ds/dados.parquet"
    (up,) = srv.uploads
    assert up["bytes"] == 3 << 20
    assert up["md5"] == md5
    assert up["headers"]["content-length"] == str(3 << 20)
    assert "transfer-encoding" not in up["headers"]
    assert up["headers"]["x-goog-hash"] == f"md5={md5}"


async def test_retentativa_reenvia_o_arquivo_inteiro(tmp_path, monkeypatch):
    """One attempt's iterator cannot be re-read: the factory reopens the file on each one."""
    monkeypatch.setattr(A.asyncio, "sleep", lambda *_a, **_k: _nada())
    p, md5 = _arquivo(tmp_path, 2)
    srv = _Servidor(falhas=2)
    await _lake(monkeypatch, srv).gravar_arquivo("gold", "ds/dados.parquet", p)
    assert [u["bytes"] for u in srv.uploads] == [2 << 20] * 3
    assert {u["md5"] for u in srv.uploads} == {md5}


async def _nada():
    return None


async def test_arquivo_pequeno_segue_o_caminho_de_bytes(tmp_path, monkeypatch):
    p = tmp_path / "m.json"
    p.write_bytes(b'{"a": 1}')
    srv = _Servidor()
    await _lake(monkeypatch, srv).gravar_arquivo("gold", "ds/m.json", p)
    (up,) = srv.uploads
    assert up["bytes"] == 8


async def test_pico_de_memoria_nao_cresce_com_o_arquivo(tmp_path, monkeypatch):
    """`read_bytes` put the whole object in memory (peak >= file size). With streaming the peak is of
    the order of the block, and does not change from 8 to 32 MiB."""
    lake = _lake(monkeypatch, _Servidor())
    picos = {}
    for mib in (8, 32):
        p, _ = _arquivo(tmp_path, mib)
        await lake.gravar_arquivo("gold", f"ds/{mib}.parquet", p)  # warms up token and client
        tracemalloc.start()
        await lake.gravar_arquivo("gold", f"ds/{mib}b.parquet", p)
        picos[mib] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert picos[32] < 6 << 20, picos
    assert picos[32] < picos[8] + (2 << 20), picos
