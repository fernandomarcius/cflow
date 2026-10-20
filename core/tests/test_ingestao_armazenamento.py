"""Lake object backend: `local` and `gcs`.

Two guarantees that the rest of ingestion assumes and that are only proven here:
**immutability** (writing over is refused - landing is evidence) and **contained path** (the file
name comes from a third party; `..` cannot escape the layer).

`gcs` is tested through a simulated transport: it proves the SHAPE of the request (generation
precondition, CMEK, scope) without depending on a credential or the network - and without spending
a cent of cloud.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import armazenamento as A


@pytest.fixture(autouse=True)
def _gcs_permitido_neste_modulo(monkeypatch):
    """This module exercises the GCS backend against a SIMULATED transport (no request leaves), so it
    turns the `CFLOW_LAKE_SOMENTE_LOCAL` guard off explicitly: the developer `.env` turns it on, and
    the test cannot depend on which `.env` exists on the machine. The guard own tests turn it back
    on."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_SOMENTE_LOCAL", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Local ---------------------------------------------------------------------------------


async def test_local_grava_e_le(tmp_path) -> None:
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    uri = await lake.gravar("landing", "ds/ingestion_date=2026-08-15/2026-08/abc/a.csv", b"x;y")
    assert uri.startswith("file://")
    assert await lake.ler("landing", "ds/ingestion_date=2026-08-15/2026-08/abc/a.csv") == b"x;y"
    assert await lake.existe("landing", "ds/ingestion_date=2026-08-15/2026-08/abc/a.csv")


async def test_local_recusa_sobrescrever(tmp_path) -> None:
    """Landing is immutable: overwriting erases the evidence it exists to keep."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("landing", "ds/a.csv", b"original")
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("landing", "ds/a.csv", b"other")
    assert e.value.codigo == "objeto_ja_existe" and e.value.status == 409
    assert await lake.ler("landing", "ds/a.csv") == b"original"


async def test_local_nao_deixa_parcial_ao_falhar(tmp_path) -> None:
    """Write to a temp file + rename: a collision cannot leave a `.parcial` behind."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("landing", "ds/a.csv", b"original")
    with pytest.raises(ErroAplicacao):
        await lake.gravar("landing", "ds/a.csv", b"other")
    assert not list((tmp_path / "landing" / "ds").glob(".*parcial"))


async def test_local_queda_antes_da_publicacao_nao_deixa_destino_vazio(
    tmp_path, monkeypatch
) -> None:
    """A failure in the atomic publish leaves only a removed temp file; a rerun can win."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    destino = tmp_path / "gold" / "ds" / "_manifesto.json"
    link_original = A.os.link

    def _queda(_origem, alvo, **kwargs) -> None:
        if alvo == destino.name:
            raise RuntimeError("simulated crash")
        link_original(_origem, alvo, **kwargs)

    monkeypatch.setattr(A.os, "link", _queda)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await lake.gravar("gold", "ds/_manifesto.json", b'{"ok":true}')
    assert not destino.exists(), "a failure cannot leave a zero-byte immutable placeholder"
    assert not list((tmp_path / "gold" / "ds").glob(".*parcial"))

    monkeypatch.setattr(A.os, "link", link_original)
    await lake.gravar("gold", "ds/_manifesto.json", b'{"ok":true}')
    assert destino.read_bytes() == b'{"ok":true}'


async def test_local_recusa_ler_materializar_ou_gravar_por_symlink_fora_da_raiz(tmp_path) -> None:
    raiz = tmp_path / "lake"
    camada = raiz / "gold"
    externo = tmp_path / "outside"
    camada.mkdir(parents=True)
    externo.mkdir()
    (externo / "secret.json").write_bytes(b"secret")
    (camada / "ref").symlink_to(externo, target_is_directory=True)
    lake = A.ArmazenamentoLocal(raiz=raiz)

    for operacao in (
        lake.ler("gold", "ref/secret.json"),
        lake.materializar("gold", "ref/secret.json", tmp_path / "cache.json"),
        lake.gravar("gold", "ref/novo.json", b"cannot escape"),
    ):
        with pytest.raises(ErroAplicacao) as erro:
            await operacao
        assert erro.value.codigo == "caminho_inseguro"
    assert not (externo / "novo.json").exists()


async def test_local_swap_para_symlink_entre_validacao_e_publish_nao_escapa(
    tmp_path, monkeypatch
) -> None:
    """`dir_fd` keeps publishing in the opened directory, even when the name becomes a symlink."""
    raiz = tmp_path / "lake"
    externo = tmp_path / "outside"
    externo.mkdir()
    lake = A.ArmazenamentoLocal(raiz=raiz)
    caminho = "ref/05/summary/execucao_id=abc/_manifesto.json"
    link_original = A.os.link
    trocou = False

    def _link_com_swap(origem, destino, **kwargs) -> None:
        nonlocal trocou
        if destino == "_manifesto.json" and not trocou:
            trocou = True
            (raiz / "gold" / "ref").rename(raiz / "gold" / "ref_validada")
            (raiz / "gold" / "ref").symlink_to(externo, target_is_directory=True)
        link_original(origem, destino, **kwargs)

    monkeypatch.setattr(A.os, "link", _link_com_swap)
    with pytest.raises(ErroAplicacao) as erro:
        await lake.gravar("gold", caminho, b"race-proof")

    assert trocou
    assert erro.value.codigo == "caminho_inseguro"
    assert not (externo / "05" / "summary" / "execucao_id=abc" / "_manifesto.json").exists()
    seguro = raiz / "gold" / "ref_validada" / "05" / "summary" / "execucao_id=abc"
    assert (seguro / "_manifesto.json").read_bytes() == b"race-proof"


@pytest.mark.parametrize("ruim", ["../fora.csv", "ds/../../etc/passwd", "", "ds\\..\\fora.csv"])
async def test_caminho_nao_escapa_da_camada(tmp_path, ruim: str) -> None:
    """The file name comes from a third party (URL basename, portal link): `..` is refused."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("landing", ruim, b"x")
    assert e.value.codigo == "caminho_invalido"


async def test_barra_inicial_e_normalizada_nao_recusada(tmp_path) -> None:
    """`/a.csv` is the same object as `a.csv` INSIDE the layer - normalising is right; refusing
    would turn a formatting detail into an ingestion failure."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("landing", "/ds/a.csv", b"x")
    assert await lake.existe("landing", "ds/a.csv")
    assert (tmp_path / "landing" / "ds" / "a.csv").is_file()


async def test_camada_invalida_recusada(tmp_path) -> None:
    with pytest.raises(ErroAplicacao) as e:
        await A.ArmazenamentoLocal(raiz=tmp_path).gravar("bronze_x", "a.csv", b"x")
    assert e.value.codigo == "camada_invalida"


async def test_gold_aceita_reescrita_como_a_silver(tmp_path) -> None:
    """Gold is derived like silver: the pointer is rewritten, the per-run data is not.

    It is separate from silver because it holds a CONFORMED product (joining several tables and a
    recalculation); putting that in the silver bucket would make silver consumers find a dataset that
    follows none of its rules."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("gold", "orders/_atual.json", b'{"v":1}')
    await lake.gravar("gold", "orders/_atual.json", b'{"v":2}', sobrescrever=True)
    assert await lake.ler("gold", "orders/_atual.json") == b'{"v":2}'


async def test_silver_aceita_reescrita(tmp_path) -> None:
    """Silver is derived: rewriting its pointer is the expected behaviour."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("silver", "ds/_atual.json", b'{"v":1}')
    await lake.gravar("silver", "ds/_atual.json", b'{"v":2}', sobrescrever=True)
    assert await lake.ler("silver", "ds/_atual.json") == b'{"v":2}'


async def test_silver_sem_a_flag_continua_recusando(tmp_path) -> None:
    """Rewriting is always EXPLICIT, even in the layer that allows it."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("silver", "ds/dados.parquet", b"a")
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("silver", "ds/dados.parquet", b"b")
    assert e.value.codigo == "objeto_ja_existe"


@pytest.mark.parametrize("camada", ["landing", "bronze", "quarantine"])
async def test_camada_de_evidencia_recusa_sobrescrita_ate_se_pedida(tmp_path, camada: str) -> None:
    """Immutability belongs to the LAYER: not even a caller asking explicitly can erase the
    evidence. It is the invariant that supports the eternal cache of the bronze read."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar(camada, "ds/a.csv", b"original")
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar(camada, "ds/a.csv", b"other", sobrescrever=True)
    assert e.value.codigo == "camada_imutavel"
    assert await lake.ler(camada, "ds/a.csv") == b"original"


async def test_materializar_local_nao_copia(tmp_path) -> None:
    """Air-gapped mode pays no cache: the object is ALREADY a local file."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path / "lake")
    await lake.gravar("bronze", "ds/a.parquet", b"content")
    cache = tmp_path / "cache" / "x.parquet"
    caminho = await lake.materializar("bronze", "ds/a.parquet", cache)
    assert caminho == str(tmp_path / "lake" / "bronze" / "ds" / "a.parquet")
    assert not cache.exists(), "cannot duplicate the lake on disk"


async def test_materializar_local_de_objeto_ausente(tmp_path) -> None:
    with pytest.raises(ErroAplicacao) as e:
        await A.ArmazenamentoLocal(raiz=tmp_path).materializar(
            "bronze", "does/not/exist.parquet", tmp_path / "c.parquet"
        )
    assert e.value.codigo == "objeto_ausente"


async def test_objeto_ausente_e_404(tmp_path) -> None:
    with pytest.raises(ErroAplicacao) as e:
        await A.ArmazenamentoLocal(raiz=tmp_path).ler("bronze", "does/not/exist.parquet")
    assert e.value.status == 404


async def test_local_copia_verifica_bytes_e_reexecucao_e_idempotente(tmp_path) -> None:
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("gold", "ds/origem.parquet", b"content")
    primeira = await lake.copiar("gold", "ds/origem.parquet", "ds/copia.parquet")
    segunda = await lake.copiar("gold", "ds/origem.parquet", "ds/copia.parquet")
    assert primeira["bytes"] == 7 and primeira["ja_existia"] is False
    assert segunda == {**primeira, "ja_existia": True}
    assert await lake.ler("gold", "ds/copia.parquet") == b"content"


async def test_local_copia_recusa_destino_parcial_ou_conflitante(tmp_path) -> None:
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("gold", "ds/origem.parquet", b"certo")
    await lake.gravar("gold", "ds/copia.parquet", b"truncated")
    with pytest.raises(ErroAplicacao) as e:
        await lake.copiar("gold", "ds/origem.parquet", "ds/copia.parquet")
    assert e.value.codigo == "copia_lake_conflitante"


async def test_local_copia_nao_expoe_destino_vazio_antes_da_publicacao(
    tmp_path, monkeypatch
) -> None:
    """The final name can only appear when it already references all the source bytes."""
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("gold", "ds/origem.parquet", b"content")
    destino = tmp_path / "gold" / "ds" / "copia.parquet"
    replace_original = A.os.replace

    def _replace_sem_janela_vazia(origem, alvo) -> None:
        if A.Path(alvo) == destino:
            assert not destino.exists(), "the destination became visible empty before publishing"
        replace_original(origem, alvo)

    monkeypatch.setattr(A.os, "replace", _replace_sem_janela_vazia)
    await lake.copiar("gold", "ds/origem.parquet", "ds/copia.parquet")
    assert destino.read_bytes() == b"content"


async def test_local_copia_trata_corrida_com_destino_equivalente_como_idempotente(
    tmp_path, monkeypatch
) -> None:
    lake = A.ArmazenamentoLocal(raiz=tmp_path)
    await lake.gravar("gold", "ds/origem.parquet", b"content")
    destino = tmp_path / "gold" / "ds" / "copia.parquet"
    link_original = A.os.link
    corrida_injetada = False

    def _link_com_corrida(origem, alvo) -> None:
        nonlocal corrida_injetada
        if A.Path(alvo) == destino and not corrida_injetada:
            corrida_injetada = True
            destino.write_bytes(b"content")
            raise FileExistsError
        link_original(origem, alvo)

    monkeypatch.setattr(A.os, "link", _link_com_corrida)
    resultado = await lake.copiar("gold", "ds/origem.parquet", "ds/copia.parquet")
    assert corrida_injetada
    assert resultado["ja_existia"] is True
    assert destino.read_bytes() == b"content"


# --- GCS (simulated transport) ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sem_token_herdado():
    """The token cache is at MODULE level (what keeps a backfill from re-authenticating per slice),
    so it CROSSES tests: without this, the test that counts token exchanges would count zero because
    the previous test already authenticated with the same identity."""
    A.limpar_cache_token()
    yield
    A.limpar_cache_token()


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


def _simular(monkeypatch, handler) -> list[httpx.Request]:
    """Inject a simulated transport into EVERY `httpx.AsyncClient` created by the backend."""
    vistas: list[httpx.Request] = []

    def _registrar(req: httpx.Request) -> httpx.Response:
        vistas.append(req)
        return handler(req)

    original = httpx.AsyncClient

    def _fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_registrar)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _fabrica)
    return vistas


def _lake_gcs(**over) -> A.ArmazenamentoGcs:
    config = {
        "provedor": "gcs",
        "buckets": {
            "landing": "example-landing",
            "bronze": "example-bronze",
            "quarantine": "example-quarantine",
            "silver": "example-silver",
        },
        **over,
    }
    return A.montar("objeto", config, json.dumps(_credencial()))


def _ok(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/token":
        return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
    return httpx.Response(200, json={"name": "objeto"})


def _metadado_gcs(*, md5: str = "md5-origem", crc32c: str = "crc-origem") -> dict:
    return {"name": "objeto", "size": "8", "md5Hash": md5, "crc32c": crc32c}


async def test_gcs_copia_nova_com_precondicao_e_confere_digests(monkeypatch) -> None:
    meta = _metadado_gcs()

    def _copia(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "copyTo" in req.url.path:
            return httpx.Response(200, json=meta)
        if req.url.path.endswith("origem.parquet"):
            return httpx.Response(200, json=meta)
        return httpx.Response(404)

    vistas = _simular(monkeypatch, _copia)
    resultado = await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    copia = next(r for r in vistas if "copyTo" in r.url.path)
    assert copia.url.params["ifGenerationMatch"] == "0"
    assert resultado == {
        "bytes": 8,
        "md5": "md5-origem",
        "crc32c": "crc-origem",
        "ja_existia": False,
    }


async def test_gcs_copia_preexistente_equivalente_e_idempotente(monkeypatch) -> None:
    meta = _metadado_gcs()

    def _existente(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json=meta)

    vistas = _simular(monkeypatch, _existente)
    resultado = await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    assert resultado["ja_existia"] is True
    assert not any("copyTo" in r.url.path for r in vistas)


async def test_gcs_copia_aceita_crc32c_igual_quando_md5_nao_existe(monkeypatch) -> None:
    meta = {"name": "objeto", "size": "8", "crc32c": "crc-origem"}

    def _so_crc32c(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json=meta)

    _simular(monkeypatch, _so_crc32c)
    resultado = await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    assert resultado["crc32c"] == "crc-origem"
    assert resultado["md5"] is None
    assert resultado["ja_existia"] is True


async def test_gcs_copia_preexistente_com_digest_divergente_e_conflito(monkeypatch) -> None:
    respostas = iter(
        [
            _metadado_gcs(),
            _metadado_gcs(md5="md5-diferente", crc32c="crc-diferente"),
        ]
    )

    def _divergente(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json=next(respostas))

    _simular(monkeypatch, _divergente)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    assert e.value.codigo == "copia_lake_conflitante"


async def test_gcs_copia_nao_aceita_mesmo_tamanho_sem_digest(monkeypatch) -> None:
    sem_digest = {"name": "objeto", "size": "8"}

    def _sem_digest(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json=sem_digest)

    _simular(monkeypatch, _sem_digest)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    assert e.value.codigo == "copia_lake_conflitante"


async def test_gcs_copia_412_rele_e_comprova_destino_da_corrida(monkeypatch) -> None:
    meta = _metadado_gcs()
    leituras_destino = 0

    def _corrida(req: httpx.Request) -> httpx.Response:
        nonlocal leituras_destino
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "copyTo" in req.url.path:
            return httpx.Response(412)
        if req.url.path.endswith("origem.parquet"):
            return httpx.Response(200, json=meta)
        leituras_destino += 1
        return httpx.Response(404 if leituras_destino == 1 else 200, json=meta)

    _simular(monkeypatch, _corrida)
    resultado = await _lake_gcs().copiar("silver", "ds/origem.parquet", "ds/copia.parquet")
    assert leituras_destino == 2
    assert resultado["ja_existia"] is True


async def test_gcs_grava_com_precondicao_de_imutabilidade(monkeypatch) -> None:
    """`ifGenerationMatch=0` is immutability on the GCS side: it only creates, never replaces."""
    vistas = _simular(monkeypatch, _ok)
    uri = await _lake_gcs().gravar("landing", "ds/a.csv", b"content", tipo_mime="text/csv")

    upload = [r for r in vistas if "upload" in r.url.path][0]
    assert upload.url.params["ifGenerationMatch"] == "0"
    assert upload.url.params["name"] == "ds/a.csv"
    assert upload.headers["Authorization"] == "Bearer tok-123"
    assert upload.headers["Content-Type"] == "text/csv"
    assert uri == "gs://example-landing/ds/a.csv"


async def test_gcs_reusa_o_cliente_http_entre_requisicoes(monkeypatch) -> None:
    """Keep-alive: five objects written (plus the token exchange) with ONE HTTP client. Before, each
    request opened and closed its own, and every object paid TCP + TLS again."""
    criados: list = []
    original = httpx.AsyncClient

    def _fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_ok)
        cli = original(*args, **kwargs)
        criados.append(cli)
        return cli

    monkeypatch.setattr(httpx, "AsyncClient", _fabrica)
    lake = _lake_gcs()
    for i in range(5):
        await lake.gravar("landing", f"ds/{i}.csv", b"x", tipo_mime="text/csv")
    assert len(criados) == 1


async def test_gcs_412_vira_objeto_ja_existe(monkeypatch) -> None:
    def _conflito(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(412, json={"error": "precondition"})

    _simular(monkeypatch, _conflito)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "objeto_ja_existe"


async def test_gcs_cmek_por_objeto(monkeypatch) -> None:
    vistas = _simular(monkeypatch, _ok)
    await _lake_gcs(chave_kms="projects/p/locations/us-east1/keyRings/k/cryptoKeys/c").gravar(
        "bronze", "ds/a.parquet", b"x"
    )
    upload = [r for r in vistas if "upload" in r.url.path][0]
    assert upload.url.params["kmsKeyName"].endswith("/cryptoKeys/c")


async def test_gcs_reusa_o_token_entre_objetos(monkeypatch) -> None:
    """Re-authenticating per object is what makes a backfill hit 429."""
    vistas = _simular(monkeypatch, _ok)
    lake = _lake_gcs()
    await lake.gravar("landing", "ds/a.csv", b"x")
    await lake.gravar("landing", "ds/b.csv", b"y")
    assert len([r for r in vistas if r.url.path == "/token"]) == 1


async def test_gcs_pede_escopo_de_storage(monkeypatch) -> None:
    vistas = _simular(monkeypatch, _ok)
    await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    token = [r for r in vistas if r.url.path == "/token"][0]
    corpo = token.content.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in corpo
    assert "assertion=" in corpo


async def test_gcs_bucket_de_camada_nao_configurado(monkeypatch) -> None:
    _simular(monkeypatch, _ok)
    lake = A.ArmazenamentoGcs(buckets={"landing": "b"}, credencial=_credencial())
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("bronze", "a.parquet", b"x")
    assert e.value.codigo == "bucket_nao_configurado"


async def test_gcs_prefixo_entra_no_caminho(monkeypatch) -> None:
    vistas = _simular(monkeypatch, _ok)
    await _lake_gcs(prefixo="example/").gravar("landing", "ds/a.csv", b"x")
    upload = [r for r in vistas if "upload" in r.url.path][0]
    assert upload.url.params["name"] == "example/ds/a.csv"


# --- Factory / connection validation -----------------------------------------------------------


def test_validar_config_exige_quarentena() -> None:
    """Without a dead-letter bucket, a malformed batch vanishes - exactly what needs looking at."""
    problemas = A.validar_config({"provedor": "gcs", "buckets": {"landing": "a", "bronze": "b"}})
    assert any("quarantine" in p for p in problemas)


def test_validar_config_local_ok() -> None:
    assert A.validar_config({"provedor": "local", "raiz": "/var/lake"}) == []


def test_montar_recusa_tipo_de_conexao_errado() -> None:
    with pytest.raises(ErroAplicacao) as e:
        A.montar("postgres", {}, None)
    assert e.value.codigo == "conexao_tipo_sem_lake"


def test_montar_gcs_sem_credencial() -> None:
    with pytest.raises(ErroAplicacao) as e:
        A.montar("objeto", {"provedor": "gcs", "buckets": {"landing": "a"}}, None)
    assert e.value.codigo == "credencial_ausente"


def test_montar_gcs_com_credencial_que_nao_e_json() -> None:
    with pytest.raises(ErroAplicacao) as e:
        A.montar("objeto", {"provedor": "gcs", "buckets": {"landing": "a"}}, "not-json")
    assert e.value.codigo == "credencial_invalida"


async def test_gcs_403_no_bucket_nao_reprova_a_credencial(monkeypatch) -> None:
    """The ingestion service account has OBJECT permission (`objectAdmin`/`objectCreator`), so
    `GET /b/{bucket}` answers 403 even with a perfectly good write credential. Failing on that is
    failing the right configuration."""

    def _sem_metadado(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(403, json={"error": "forbidden"})

    _simular(monkeypatch, _sem_metadado)
    r = await _lake_gcs().testar()
    assert r["ok"] is True
    assert "credential accepted" in r["buckets"]["landing"]


async def test_gcs_bucket_inexistente_reprova(monkeypatch) -> None:
    def _sem_bucket(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(404, json={"error": "not found"})

    _simular(monkeypatch, _sem_bucket)
    r = await _lake_gcs().testar()
    assert r["ok"] is False and "does not exist" in r["causa"]


async def test_gcs_materializar_baixa_uma_vez_so(monkeypatch, tmp_path) -> None:
    """The bronze path is content-addressed and the layer is immutable - the cache NEVER goes stale.
    Re-downloading on every read is what would make a pipeline over 32 slices cost 32 downloads."""

    def _com_conteudo(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=b"PARQUET-BYTES")

    vistas = _simular(monkeypatch, _com_conteudo)
    lake = _lake_gcs()
    destino = tmp_path / "cache" / "abc.parquet"

    p1 = await lake.materializar("bronze", "ds/2026-08/abc/dados.parquet", destino)
    baixadas = len([r for r in vistas if r.url.params.get("alt") == "media"])
    p2 = await lake.materializar("bronze", "ds/2026-08/abc/dados.parquet", destino)

    assert p1 == p2 == str(destino)
    assert destino.read_bytes() == b"PARQUET-BYTES"
    assert baixadas == 1
    assert len([r for r in vistas if r.url.params.get("alt") == "media"]) == 1, (
        "the 2nd time does not download"
    )


def test_cache_separa_lotes_que_compartilham_o_sha(tmp_path, monkeypatch) -> None:
    """A ZIP can contain nine tables of different grains: nine bronze datasets, nine Parquets - and
    **a single `sha256`**, because the sha belongs to the payload DOWNLOADED FROM THE ORIGIN, not to
    the written file.

    Keyed by sha, the cache served the first downloaded file to all nine: one dataset read the
    content of another, and the generator produced the same schema for tables with no column in
    common. The object in the bucket was always right - it was the read that confused them, and
    silently. The cache key must be the object PATH."""
    from cflow_core.config import get_settings
    from cflow_core.ingestao.leitura import _cache_de

    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        sha12 = "8b19d3c1200a"  # the same in both: it came from the same ZIP
        a = _cache_de(f"registry_general/ingestion_date=2026-08-15/2026/{sha12}/dados.parquet")
        b = _cache_de(f"registry_portfolio/ingestion_date=2026-08-15/2026/{sha12}/dados.parquet")
        assert a != b
    finally:
        get_settings.cache_clear()


def test_cache_recusa_caminho_que_sai_da_raiz(tmp_path, monkeypatch) -> None:
    """The path comes from the `bronze_uri` stored in the database. Mirroring it on disk without
    checking would let a `..` write outside the cache directory."""
    from cflow_core.config import get_settings
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao.leitura import _cache_de

    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with pytest.raises(ErroAplicacao) as e:
            _cache_de("../../etc/passwd")
        assert e.value.codigo == "caminho_de_lote_invalido"
    finally:
        get_settings.cache_clear()


async def test_gcs_ponteiro_da_silver_sobrescreve(monkeypatch) -> None:
    """`ifGenerationMatch=0` is omitted only when rewriting is requested: it is what makes swapping
    the silver pointer an atomic replacement instead of a 412."""
    vistas = _simular(monkeypatch, _ok)
    await _lake_gcs().gravar("silver", "ds/_atual.json", b"{}", sobrescrever=True)
    upload = [r for r in vistas if "upload" in r.url.path][0]
    assert "ifGenerationMatch" not in upload.url.params


async def test_gcs_bronze_recusa_sobrescrita_antes_de_qualquer_rede(monkeypatch) -> None:
    vistas = _simular(monkeypatch, _ok)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("bronze", "ds/a.csv", b"x", sobrescrever=True)
    assert e.value.codigo == "camada_imutavel"
    assert vistas == [], "does not even authenticate: the refusal comes before the network"


async def test_gcs_silver_sem_bucket_configurado_e_erro_nomeado(monkeypatch) -> None:
    """Whoever only ingests does not need the silver bucket - but publishing without it must say
    what is missing, not blow up in the middle of the upload."""
    _simular(monkeypatch, _ok)
    lake = A.ArmazenamentoGcs(
        buckets={"landing": "l", "bronze": "b", "quarantine": "q"}, credencial=_credencial()
    )
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("silver", "ds/a.parquet", b"x")
    assert e.value.codigo == "bucket_nao_configurado"
    assert "silver" in e.value.mensagem


# --- Durability: token cache, retry and checksum --------------------------------------


def _handler_sequencia(respostas: list) -> tuple:
    """Return `(handler, counter)`: the handler serves `respostas` in order for the calls that are
    NOT token calls, repeating the last one when the list runs out."""
    chamadas: list[int] = []

    def _h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        i = min(len(chamadas), len(respostas) - 1)
        chamadas.append(i)
        return respostas[i]

    return _h, chamadas


def _sem_dormir(monkeypatch) -> list[float]:
    """Neutralise the backoff wait and return the list of requested values - the test must prove
    HOW MUCH, not spend the time."""
    dormidas: list[float] = []

    async def _fake(segundos: float) -> None:
        dormidas.append(segundos)

    monkeypatch.setattr(A.asyncio, "sleep", _fake)
    return dormidas


def _uploads(vistas: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in vistas if "upload" in r.url.path]


async def test_gcs_retenta_503_e_grava(monkeypatch) -> None:
    """A 503 is an infrastructure hiccup: retrying costs ONE request to GCS. Not retrying costs the
    workflow engine redoing the whole SLICE - and with it the download of the public origin file,
    which is how an IP gets blocked."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [
            httpx.Response(503, json={"error": "backend"}),
            httpx.Response(503, json={"error": "backend"}),
            httpx.Response(200, json={"name": "objeto"}),
        ]
    )
    vistas = _simular(monkeypatch, handler)
    uri = await _lake_gcs().gravar("landing", "ds/a.csv", b"content")
    assert uri == "gs://example-landing/ds/a.csv"
    assert len(_uploads(vistas)) == 3


async def test_gcs_403_nao_retenta(monkeypatch) -> None:
    """A 403 is a verdict on the REQUEST, not on the moment: repeating does not change permission,
    it only multiplies denied requests."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia([httpx.Response(403, json={"error": "forbidden"})])
    vistas = _simular(monkeypatch, handler)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "falha_gravacao_lake"
    assert len(_uploads(vistas)) == 1, "a 403 cannot become four requests"


async def test_gcs_412_nao_retenta(monkeypatch) -> None:
    """A 412 is `ifGenerationMatch=0` working. Insisting is asking immutability to stop applying."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia([httpx.Response(412, json={"error": "precondition"})])
    vistas = _simular(monkeypatch, handler)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "objeto_ja_existe"
    assert len(_uploads(vistas)) == 1


async def test_gcs_412_na_retentativa_vira_objeto_ja_existe(monkeypatch) -> None:
    """The case that the retry CREATES: the PUT really wrote and the response was lost; the second
    attempt finds the object there and gets a 412. It must come out as `objeto_ja_existe` - which is
    what `motor._gravar_se_novo` swallows as a no-op -, never as a write failure, otherwise the
    slice would be stuck for having succeeded."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [httpx.Response(503, json={}), httpx.Response(412, json={"error": "precondition"})]
    )
    vistas = _simular(monkeypatch, handler)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "objeto_ja_existe" and e.value.status == 409
    assert len(_uploads(vistas)) == 2


async def test_gcs_retentativa_de_sobrescrita_e_idempotente(monkeypatch) -> None:
    """The other half of idempotency under retry. Silver/gold publications
    (`ingestao/leitura.py`) write with `sobrescrever=True`, that is WITHOUT `ifGenerationMatch` - no
    412 is possible there, and the retry is the same atomic replacement of the SAME bytes.

    What the test pins is that the second attempt does not acquire the precondition by accident: if
    it did, the retry of a 503 on the pointer swap would start failing with 412 exactly when the
    pointer was already there."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [httpx.Response(503, json={}), httpx.Response(200, json={"name": "o"})]
    )
    vistas = _simular(monkeypatch, handler)
    await _lake_gcs().gravar("silver", "ds/_atual.json", b"{}", sobrescrever=True)
    uploads = _uploads(vistas)
    assert len(uploads) == 2
    assert all("ifGenerationMatch" not in u.url.params for u in uploads)
    assert all(u.content == b"{}" for u in uploads), "the retry sends the SAME bytes"


async def test_gcs_honra_retry_after(monkeypatch) -> None:
    """When the server SAYS when to come back, it knows more than our exponential (it is the one
    that knows the quota). The cap still applies - a one-hour `Retry-After` would hold the activity
    hostage."""
    dormidas = _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [
            httpx.Response(429, headers={"Retry-After": "7"}, json={}),
            httpx.Response(200, json={"name": "objeto"}),
        ]
    )
    _simular(monkeypatch, handler)
    await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert len(dormidas) == 1
    assert 7.0 <= dormidas[0] < 7.6, f"expected ~7 s (the server request), got {dormidas[0]}"


async def test_gcs_retry_after_absurdo_fica_no_teto(monkeypatch) -> None:
    dormidas = _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [
            httpx.Response(503, headers={"Retry-After": "3600"}, json={}),
            httpx.Response(200, json={"name": "objeto"}),
        ]
    )
    _simular(monkeypatch, handler)
    await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert dormidas[0] < 21.0, "without a cap, an activity sleeps for an hour"


async def test_gcs_reautentica_uma_vez_no_401(monkeypatch) -> None:
    """The token may expire between the margin check and the call. One forced re-authentication
    solves it; from the second on, a 401 is a wrong credential, and insisting only multiplies token
    requests."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [httpx.Response(401, json={"error": "expired"}), httpx.Response(200, json={})]
    )
    vistas = _simular(monkeypatch, handler)
    await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert len([r for r in vistas if r.url.path == "/token"]) == 2
    assert len(_uploads(vistas)) == 2


async def test_gcs_401_persistente_desiste(monkeypatch) -> None:
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia([httpx.Response(401, json={"error": "expired"})])
    vistas = _simular(monkeypatch, handler)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "falha_gravacao_lake"
    assert len(_uploads(vistas)) == 2, "one re-authentication, not four"


async def test_gcs_falha_de_transporte_vira_erro_de_infra(monkeypatch) -> None:
    """A network outage is INFRASTRUCTURE: it comes out with the same code as always (the workflow
    engine retry fixes it), with a sanitised cause - the lake URL does not go to the audit trail."""
    _sem_dormir(monkeypatch)

    def _cai(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        raise httpx.ConnectError("no route", request=req)

    _simular(monkeypatch, _cai)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().ler("bronze", "ds/a.parquet")
    assert e.value.codigo == "falha_ler_lake" and e.value.status == 502
    assert "storage.googleapis" not in e.value.mensagem


async def test_gcs_manda_o_md5_no_upload(monkeypatch) -> None:
    """`x-goog-hash` makes GCS validate on its side and refuse with a 400 if the bytes do not match -
    it keeps a truncated object from landing in the lake looking complete."""
    import base64
    import hashlib

    vistas = _simular(monkeypatch, _ok)
    dados = b"parquet content"
    await _lake_gcs().gravar("landing", "ds/a.csv", dados)
    upload = _uploads(vistas)[0]
    esperado = base64.b64encode(hashlib.md5(dados, usedforsecurity=False).digest()).decode()
    assert upload.headers["x-goog-hash"] == f"md5={esperado}"


async def test_gcs_confirmacao_do_upload_divergente_levanta(monkeypatch) -> None:
    """The upload body carries `md5Hash`/`size` and used to be discarded. Checking is free and
    catches the case where `x-goog-hash` was not honoured by something on the way - exactly when
    server-side validation does not protect. And it is INFRASTRUCTURE, not data: the bytes were
    hashed by us before leaving, so the transport is what changed."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia(
        [
            httpx.Response(
                200, json={"name": "o", "md5Hash": "AAAAAAAAAAAAAAAAAAAAAA==", "size": "3"}
            )
        ]
    )
    _simular(monkeypatch, handler)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().gravar("landing", "ds/a.csv", b"content")
    assert e.value.codigo == "falha_gravacao_lake"
    assert "divergent checksum" in e.value.mensagem


async def test_gcs_leitura_confere_o_x_goog_hash(monkeypatch) -> None:
    """Closes `materializar` without a new database column: `ingestao_lotes.sha256` is the hash of the
    RAW origin payload, before decoding - not of the bronze Parquet. The only Parquet digest that
    exists is the one GCS itself declares in the response."""
    _sem_dormir(monkeypatch)

    def _corrompido(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(
            200,
            content=b"SWAPPED-BYTES",
            headers={"x-goog-hash": "crc32c=AAAAAA==,md5=1B2M2Y8AsgTpgAmY7PhCfg=="},
        )

    _simular(monkeypatch, _corrompido)
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().ler("bronze", "ds/a.parquet")
    assert e.value.codigo == "falha_ler_lake"
    assert "Checksum mismatch" in e.value.mensagem


async def test_gcs_leitura_boa_passa_no_checksum(monkeypatch) -> None:
    import base64
    import hashlib

    dados = b"PARQUET-BYTES"
    md5 = base64.b64encode(hashlib.md5(dados, usedforsecurity=False).digest()).decode()

    def _integro(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=dados, headers={"x-goog-hash": f"md5={md5}"})

    _simular(monkeypatch, _integro)
    assert await _lake_gcs().ler("bronze", "ds/a.parquet") == dados


async def test_gcs_leitura_sem_o_header_nao_inventa_falha(monkeypatch) -> None:
    """Not every response carries `x-goog-hash`, and one carrying only `crc32c` gives nothing to
    check. Failing on a missing header would turn a good read into an infrastructure error."""

    def _sem_hash(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=b"X", headers={"x-goog-hash": "crc32c=AAAAAA=="})

    _simular(monkeypatch, _sem_hash)
    assert await _lake_gcs().ler("bronze", "ds/a.parquet") == b"X"


async def test_gcs_duas_instancias_da_mesma_credencial_fazem_uma_troca(monkeypatch) -> None:
    """The token cache MUST be at module level: `armazenamento.abrir()` builds a new instance on every
    call and ingestion calls it once PER SLICE. With the cache on the instance, a ten-year monthly
    backfill performed 120 RS256 signatures and 120 jwt-bearer exchanges.

    Note that the two instances have DIFFERENT private keys (each `_credencial()` generates its own)
    and the same `client_email`/`token_uri`: the cache key is the IDENTITY, never private-key
    material - which must not become a dictionary key nor appear in a traceback."""
    vistas = _simular(monkeypatch, _ok)
    await _lake_gcs().gravar("landing", "ds/a.csv", b"x")
    await _lake_gcs().gravar("landing", "ds/b.csv", b"y")
    assert len([r for r in vistas if r.url.path == "/token"]) == 1


async def test_gcs_modo_token_nao_reautentica_no_401(monkeypatch) -> None:
    """In `token` mode the secret in the store ALREADY is the access token: there is no exchange to
    redo, and the message must say what to do (rotate), not "failed to write"."""
    _sem_dormir(monkeypatch)
    handler, _ = _handler_sequencia([httpx.Response(401, json={"error": "expired"})])
    vistas = _simular(monkeypatch, handler)
    lake = A.montar(
        "objeto",
        {"provedor": "gcs", "autenticacao": "token", "buckets": {"landing": "b"}},
        "ya29.one-hour-token",
    )
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("landing", "ds/a.csv", b"x")
    assert e.value.codigo == "token_lake_expirado"
    assert len(_uploads(vistas)) == 1
    assert vistas == _uploads(vistas), "token mode exchanges nothing with oauth"


# --- Shared backoff helper -----------------------------------------------------------


def test_retry_after_em_segundos_e_em_data() -> None:
    from cflow_core.retry import retry_after_segundos

    assert retry_after_segundos("120") == 120.0
    assert retry_after_segundos(None) is None
    assert retry_after_segundos("not that") is None
    # An HTTP date without a timezone is GMT by definition: assuming local time would give hours of wait.
    passado = retry_after_segundos("Wed, 21 Oct 2015 07:28:00 GMT")
    assert passado == 0.0, "a date in the past means 'go now', never a negative sleep"


def test_espera_mantem_a_formula_dos_chamadores_antigos() -> None:
    """The two callers that already existed (`ingestao/conectores/base.py` and
    `conexoes/conectores/api_query.py`) were refactored onto this helper WITHOUT any behaviour
    change - it is the same `min(2**attempt, 20) + jitter`."""
    from cflow_core.retry import espera

    for tentativa, base in ((0, 1.0), (1, 2.0), (3, 8.0), (10, 20.0)):
        v = espera(tentativa)
        assert base <= v < base + 0.5, f"attempt {tentativa}: {v}"


def test_retentaveis_exclui_o_412() -> None:
    """A 412 is `ifGenerationMatch=0` refusing because the object already exists: it is the RIGHT
    answer."""
    from cflow_core.retry import RETENTAVEIS

    for codigo in (400, 401, 403, 404, 409, 412):
        assert codigo not in RETENTAVEIS
    for codigo in (408, 429, 500, 502, 503, 504):
        assert codigo in RETENTAVEIS


async def test_gcs_materializar_concorrente_baixa_uma_vez_so(monkeypatch, tmp_path) -> None:
    """Ten references reading the same dataset in the same second made ten downloads: the cache only
    sees the file after it exists. With a single flight per destination, whoever arrives later
    waits and finds the file. The lower half proves that the test discriminates: without the
    single flight, it downloads twice."""

    # `trava` turns on, in the second half, the wait that makes the first download only answer when
    # the SECOND request arrives. It lives here, and not in a new transport, because the backend
    # reuses the HTTP client between requests: calling `_simular` again does not replace the
    # transport of one that already exists (the new transport saw no request at all).
    trava = {"ligada": False}
    dois_pedidos = asyncio.Event()
    pedidos_segurados = 0

    async def _com_conteudo(req: httpx.Request) -> httpx.Response:
        nonlocal pedidos_segurados
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if trava["ligada"]:
            pedidos_segurados += 1
            if pedidos_segurados >= 2:
                dois_pedidos.set()
            try:
                await asyncio.wait_for(dois_pedidos.wait(), 10)
            except TimeoutError:
                # This is what happens if the single flight keeps applying in this half: the second
                # `materializar` waits for the first instead of requesting, and the first waits for
                # a request that never comes. A NAMED failure, not a bare TimeoutError.
                raise AssertionError(
                    "the second download did not arrive: the single flight was not turned off in this half"
                ) from None
        return httpx.Response(200, content=b"PARQUET-BYTES")

    vistas = _simular(monkeypatch, _com_conteudo)
    lake = _lake_gcs()

    def _baixadas() -> int:
        return len([r for r in vistas if r.url.params.get("alt") == "media"])

    destino = tmp_path / "cache" / "ds.parquet"
    r = await asyncio.gather(
        *(lake.materializar("bronze", "ds/unit=11/dados.parquet", destino) for _ in range(4))
    )
    assert r == [str(destino)] * 4
    assert destino.read_bytes() == b"PARQUET-BYTES"
    assert _baixadas() == 1
    assert len(A.VOO_DOWNLOAD) == 0

    @contextlib.asynccontextmanager
    async def _nada(_chave):
        yield

    monkeypatch.setattr(A, "VOO_DOWNLOAD", type("SemVoo", (), {"voo": staticmethod(_nada)})())

    # Without the single flight, BOTH downloads must happen - and the lock is what makes this
    # deterministic: the first only answers after the second request arrives, so the second cannot
    # find the file in the cache. Without it this half is a RACE.
    trava["ligada"] = True
    outro = tmp_path / "cache" / "ds2.parquet"
    await asyncio.gather(
        *(lake.materializar("bronze", "ds/unit=12/dados.parquet", outro) for _ in range(2))
    )
    assert _baixadas() == 1 + 2, "without the single flight the concurrent download repeats"
    assert outro.read_bytes() == b"PARQUET-BYTES"


# --- "Local lake only" guard (CFLOW_LAKE_SOMENTE_LOCAL: dev, CI, integration harness) --------------


@pytest.fixture
def somente_local(monkeypatch):
    """The guard turned on, as in the developer `.env.example`, the CI e2e job and the integration
    harness. It runs AFTER `_gcs_permitido_neste_modulo` (autouse comes first), so it wins."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_SOMENTE_LOCAL", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_GCS_REMOTO = {
    "provedor": "gcs",
    "buckets": {
        "landing": "example-landing",
        "bronze": "example-bronze",
        "quarantine": "example-quarantine",
        "gold": "example-gold",
    },
}


def test_somente_local_recusa_gcs_antes_de_ler_a_credencial(somente_local, monkeypatch) -> None:
    """The refusal comes before everything: with or without a secret the error is the GUARD's
    (without it, a missing secret would give `credencial_ausente`), and no request leaves - it is the
    production bucket that it protects, and refusing after opening the path to it would be refusing
    too late."""
    vistas = _simular(monkeypatch, _ok)
    for segredo in (json.dumps(_credencial()), None):
        with pytest.raises(ErroAplicacao) as e:
            A.montar("objeto", _GCS_REMOTO, segredo)
        assert e.value.codigo == "lake_somente_local"
        assert e.value.status == 409
        assert "CFLOW_LAKE_SOMENTE_LOCAL" in e.value.mensagem and "'gcs'" in e.value.mensagem
    assert vistas == []


async def test_somente_local_abrir_por_conexao_recusa_e_o_default_segue_local(
    somente_local, monkeypatch, tmp_path
) -> None:
    """`abrir` is the path of EVERY consumer (lake/iceberg output, lake/publicado source, ingestion):
    through a bucket connection it is refused; with no connection, and with a `local` connection,
    the lake works."""

    class _Acesso:
        tipo, config, senha = "objeto", _GCS_REMOTO, json.dumps({"private_key": "PEM"})

    async def _obter_acesso(_cid):
        return _Acesso

    monkeypatch.setattr("cflow_core.conexoes.service.obter_acesso", _obter_acesso)
    with pytest.raises(ErroAplicacao) as e:
        await A.abrir("00000000-0000-0000-0000-000000000001")
    assert e.value.codigo == "lake_somente_local"

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    from cflow_core.config import get_settings

    get_settings.cache_clear()
    padrao = await A.abrir(None)
    assert padrao.provedor == "local"
    local = A.montar("objeto", {"provedor": "local", "raiz": str(tmp_path / "outro")}, None)
    await local.gravar("landing", "ds/a.csv", b"x")
    assert await local.ler("landing", "ds/a.csv") == b"x"


async def test_somente_local_testar_conexao_devolve_a_recusa_como_veredito(
    somente_local, monkeypatch
) -> None:
    """The "Test connection" action goes through the same `montar`: the UI receives the reason, not an
    exception."""
    from cflow_core.conexoes.conectores.objeto import ConectorObjeto

    vistas = _simular(monkeypatch, _ok)
    r = await ConectorObjeto().testar(_GCS_REMOTO, json.dumps(_credencial()))
    assert r.ok is False
    assert "CFLOW_LAKE_SOMENTE_LOCAL" in (r.causa or "")
    assert vistas == []


def test_guarda_e_opt_in_producao_sem_a_variavel_segue_no_gcs(monkeypatch) -> None:
    """Production does not define the variable and is what talks to GCS: a guard turned on by
    default would take down production ingestion on the first deploy. The CODE default is off (read
    without `.env`, because the developer `.env` turns it on) and, when off, the GCS backend still
    gets built."""
    from cflow_core.config import Settings, get_settings

    monkeypatch.delenv("CFLOW_LAKE_SOMENTE_LOCAL", raising=False)
    assert Settings(_env_file=None).lake_somente_local is False
    monkeypatch.setenv("CFLOW_LAKE_SOMENTE_LOCAL", "false")
    get_settings.cache_clear()
    assert isinstance(
        A.montar("objeto", _GCS_REMOTO, json.dumps(_credencial())), A.ArmazenamentoGcs
    )


def test_todo_backend_implementa_o_contrato_inteiro():
    """`Protocol` does NOT propagate a default implementation: the default written in `Armazenamento`
    would only apply to classes inheriting from it, and the backends are duck-typed. That is how
    `gravar_arquivo` was born only on the LOCAL backend and took down every GCS publication in
    production - with the benchmark, which runs locally, green on both batteries and the whole
    suite.

    This test is the missing safety net: a new method in the contract must exist in ALL backends."""
    from cflow_core.ingestao.armazenamento import (
        Armazenamento,
        ArmazenamentoGcs,
        ArmazenamentoLocal,
    )

    do_contrato = {
        n
        for n in dir(Armazenamento)
        if not n.startswith("_") and callable(getattr(Armazenamento, n, None))
    }
    assert do_contrato, "the contract cannot be empty - the test would be green for nothing"
    for backend in (ArmazenamentoLocal, ArmazenamentoGcs):
        faltando = sorted(n for n in do_contrato if not hasattr(backend, n))
        assert not faltando, f"{backend.__name__} does not implement: {faltando}"


# --- GCS download to the cache IN BLOCKS (`materializar`) -------------------------------------


def _md5(dados: bytes) -> str:
    import base64
    import hashlib

    return base64.b64encode(hashlib.md5(dados, usedforsecurity=False).digest()).decode()


async def test_gcs_materializar_grava_em_blocos_sem_ler_o_objeto_inteiro(
    monkeypatch, tmp_path
) -> None:
    """`materializar` used to be `ler()` + `write_bytes`: the whole parquet in memory on every cache
    miss. `GET /publicados/.../arquivo` serves large datasets through here, so the download goes
    straight to disk, in blocks, and `ler` is not called."""
    dados = bytes(range(256)) * 40  # 10 KiB

    def _com_conteudo(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=dados, headers={"x-goog-hash": f"md5={_md5(dados)}"})

    _simular(monkeypatch, _com_conteudo)
    monkeypatch.setattr(A, "BLOCO_DOWNLOAD", 1024)
    lake = _lake_gcs()

    async def _proibido(*_a, **_k):
        raise AssertionError("materializar cannot pass the whole object through memory")

    monkeypatch.setattr(lake, "ler", _proibido)
    escritas: list[int] = []
    original = A.em_thread_lake

    async def _espiao(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "write":
            escritas.append(len(args[0]))
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(A, "em_thread_lake", _espiao)
    destino = tmp_path / "cache" / "gold" / "ds.parquet"
    assert await lake.materializar("silver", "ds/x/dados.parquet", destino) == str(destino)
    assert destino.read_bytes() == dados
    assert len(escritas) == 10 and max(escritas) <= 1024
    assert list(destino.parent.glob(".*parcial")) == [], "no partial file is left"


async def test_gcs_materializar_md5_divergente_nao_deixa_arquivo(monkeypatch, tmp_path) -> None:
    _sem_dormir(monkeypatch)

    def _corrompido(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, content=b"SWAPPED-BYTES", headers={"x-goog-hash": "md5=AAAA"})

    _simular(monkeypatch, _corrompido)
    destino = tmp_path / "cache" / "ds.parquet"
    with pytest.raises(ErroAplicacao) as e:
        await _lake_gcs().materializar("silver", "ds/x/dados.parquet", destino)
    assert e.value.codigo == "falha_ler_lake"
    assert not destino.exists()
    assert list(destino.parent.glob(".*parcial")) == []


async def test_gcs_materializar_retenta_503_e_404_e_objeto_ausente(monkeypatch, tmp_path) -> None:
    _sem_dormir(monkeypatch)
    respostas = iter([503, 200])

    def _instavel(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "ausente" in req.url.path:
            return httpx.Response(404)
        codigo = next(respostas)
        return httpx.Response(codigo, content=b"OK" if codigo == 200 else b"")

    _simular(monkeypatch, _instavel)
    lake = _lake_gcs()
    destino = tmp_path / "cache" / "a.parquet"
    await lake.materializar("silver", "ds/a/dados.parquet", destino)
    assert destino.read_bytes() == b"OK"

    with pytest.raises(ErroAplicacao) as e:
        await lake.materializar("silver", "ds/ausente/dados.parquet", tmp_path / "cache" / "b")
    assert (e.value.status, e.value.codigo) == (404, "objeto_ausente")
