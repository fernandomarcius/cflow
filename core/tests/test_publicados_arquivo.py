"""Download of the published parquet (`GET /publicados/.../arquivo`) - the CORE part.

No database: the `Publicacao` is built by hand over a LOCAL lake in `tmp_path`, and what is proven
is the contract of `cflow_core.ingestao.publicados`: the selection must yield ONE file, the sha256
describes the bytes of the descriptor that goes out, the size matches what the publication
recorded, and the bytes go out in blocks - never a `read()` without a size. The edge (authorisation,
headers, 403/401) is covered by the route integration tests.
"""

from __future__ import annotations

import hashlib

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import armazenamento, publicados

EID = "11111111-2222-3333-4444-555555555555"


def _gravar(raiz, rel: str, sql: str) -> tuple[bytes, int]:
    destino = raiz / "gold" / rel
    destino.parent.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(f"COPY ({sql}) TO '{destino}' (FORMAT parquet)")
    dados = destino.read_bytes()
    return dados, len(dados)


def _pub(tmp_path, *, projects=("1201", "1202")) -> tuple[publicados.Publicacao, dict[str, bytes]]:
    raiz = tmp_path / "lake"
    particoes, conteudos = [], {}
    for project in projects:
        rel = f"ds/operacao_id=61/project_id={project}/execucao_id={EID}/dados.parquet"
        dados, n = _gravar(raiz, rel, f"SELECT {project} AS PROJECTID, range AS v FROM range(300)")
        conteudos[project] = dados
        particoes.append(
            {"particao": {"project_id": project}, "caminho": rel, "linhas": 300, "bytes": n}
        )
    pub = publicados.Publicacao(
        execucao_id=EID,
        dataset="ds",
        camada="gold",
        lake=armazenamento.ArmazenamentoLocal(raiz=raiz),
        base={"operacao_id": "61"},
        chaves_coluna=["project_id"],
        parametros={"operacao_id": "61"},
        documento={"particoes": particoes},
        particoes=particoes,
    )
    return pub, conteudos


# --- selection: ONE file ------------------------------------------------------------------------


def test_filtro_que_da_um_arquivo(tmp_path):
    pub, _ = _pub(tmp_path)
    p = publicados.arquivo_unico(pub, {"project_id": "1202"})
    assert p["particao"] == {"project_id": "1202"}


def test_varios_arquivos_e_409_nomeando_quantos_e_as_chaves(tmp_path):
    pub, _ = _pub(tmp_path)
    with pytest.raises(ErroAplicacao) as e:
        publicados.arquivo_unico(pub, {"operacao_id": "61"})
    assert e.value.status == 409
    assert e.value.codigo == "publicado_varios_arquivos"
    assert "2 published files" in e.value.mensagem and "project_id" in e.value.mensagem
    assert {(d.campo, d.mensagem) for d in e.value.detalhes} == {
        ("arquivos", "2"),
        ("chave_particao", "project_id"),
    }


def test_nenhum_arquivo_e_404_publicado_ausente(tmp_path):
    pub, _ = _pub(tmp_path)
    with pytest.raises(ErroAplicacao) as e:
        publicados.arquivo_unico(pub, {"project_id": "9999"})
    assert (e.value.status, e.value.codigo) == (404, "publicado_ausente")
    assert "project_id=9999" in e.value.mensagem


def test_parametro_de_outra_corrida_e_404_como_em_dados(tmp_path):
    pub, _ = _pub(tmp_path, projects=("1201",))
    with pytest.raises(ErroAplicacao) as e:
        publicados.arquivo_unico(pub, {"operacao_id": "62"})
    assert (e.value.status, e.value.codigo) == (404, "publicado_ausente")


def test_chave_desconhecida_e_400_como_em_dados(tmp_path):
    pub, _ = _pub(tmp_path, projects=("1201",))
    with pytest.raises(ErroAplicacao) as e:
        publicados.arquivo_unico(pub, {"projectid": "1201"})
    assert (e.value.status, e.value.codigo) == (400, "filtro_particao_invalido")


# --- open, measure and serve in blocks -------------------------------------------------------------


async def test_bytes_sha256_e_linhas_sao_os_do_arquivo_publicado(tmp_path, monkeypatch):
    monkeypatch.setattr(publicados, "BLOCO_ARQUIVO", 512)
    pub, conteudos = _pub(tmp_path)
    arq = await publicados.abrir_arquivo(pub, {"project_id": "1201"})
    assert arq.tamanho == len(conteudos["1201"])
    assert arq.sha256 == hashlib.sha256(conteudos["1201"]).hexdigest()
    assert arq.linhas == 300
    blocos = [b async for b in publicados.em_blocos(arq)]
    assert b"".join(blocos) == conteudos["1201"], "the hash must not have consumed the descriptor"
    assert len(blocos) > 1 and max(map(len, blocos)) <= 512
    assert arq.arquivo.closed, "the descriptor closes at the end of the stream"


async def test_blocos_nunca_leem_sem_tamanho(tmp_path, monkeypatch):
    """Proof that the file does not pass whole through memory: every stream `read` carries the block
    size. A `read()` without arguments (or -1) reads the whole file."""
    monkeypatch.setattr(publicados, "BLOCO_ARQUIVO", 256)
    pub, conteudos = _pub(tmp_path, projects=("1201",))
    arq = await publicados.abrir_arquivo(pub, {})

    pedidos: list = []

    class _Espiao:
        def __init__(self, real):
            self.real = real

        def read(self, *args):
            pedidos.append(args)
            return self.real.read(*args)

        def close(self):
            self.real.close()

    arq.arquivo = _Espiao(arq.arquivo)
    recebido = b"".join([b async for b in publicados.em_blocos(arq)])
    assert recebido == conteudos["1201"]
    assert pedidos and all(a and 0 < a[0] <= 256 for a in pedidos), pedidos


async def test_cliente_que_desiste_no_meio_fecha_o_descritor(tmp_path, monkeypatch):
    monkeypatch.setattr(publicados, "BLOCO_ARQUIVO", 128)
    pub, _ = _pub(tmp_path, projects=("1201",))
    arq = await publicados.abrir_arquivo(pub, {})
    gerador = publicados.em_blocos(arq)
    await gerador.__anext__()
    await gerador.aclose()
    assert arq.arquivo.closed


async def test_tamanho_diferente_do_registrado_e_502_sem_enviar_nada(tmp_path):
    """Lake sabotage: the file on disk is not what the run published. Serving it anyway would hand
    over a file that matches itself (sha and Content-Length from the disk itself) and not the run."""
    pub, _ = _pub(tmp_path, projects=("1201",))
    alvo = tmp_path / "lake" / "gold" / pub.particoes[0]["caminho"]
    with open(alvo, "ab") as f:
        f.write(b"junk")
    with pytest.raises(ErroAplicacao) as e:
        await publicados.abrir_arquivo(pub, {})
    assert (e.value.status, e.value.codigo) == (502, "publicado_divergente")


async def test_objeto_do_indice_ausente_no_lake_e_404(tmp_path):
    pub, _ = _pub(tmp_path, projects=("1201",))
    (tmp_path / "lake" / "gold" / pub.particoes[0]["caminho"]).unlink()
    with pytest.raises(ErroAplicacao) as e:
        await publicados.abrir_arquivo(pub, {})
    assert (e.value.status, e.value.codigo) == (404, "publicado_ausente")


def test_sha256_memorizado_pela_identidade_do_arquivo_e_volta_ao_inicio(tmp_path):
    caminho = tmp_path / "a.bin"
    caminho.write_bytes(b"x" * 3000)
    publicados._SHA_POR_ARQUIVO.clear()
    a1, n1, s1 = publicados._abrir_e_medir(str(caminho))
    a2, n2, s2 = publicados._abrir_e_medir(str(caminho))
    try:
        assert (n1, s1) == (n2, s2) == (3000, hashlib.sha256(b"x" * 3000).hexdigest())
        assert len(publicados._SHA_POR_ARQUIVO) == 1
        assert a2.read() == b"x" * 3000, "memoised or not, the descriptor returns to the start"
    finally:
        a1.close()
        a2.close()
