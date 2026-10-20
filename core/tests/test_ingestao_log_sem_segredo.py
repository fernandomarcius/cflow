"""Structured ingestion log (`cflow.ingestao.*`) - and the proof that it does not leak secrets.

The module used to be 100% silent: everything known about a run came from what was PERSISTED
afterwards in `ingestao_fatias`/`ingestao_quarentena`. Database down, worker dead or a slice that
never got to write a single row: silence.

The risk of turning the log on here is specific: the `rest_api` and `odata` connectors carry an
**access key in the querystring**, and at least one repository error message quotes the whole URL
(`download_vazio` builds "The source returned an empty body (<url>)."). A log line is copied,
pasted and exported far more easily than the audit trail - hence this file mirrors
`api/tests/integration/test_auth_sem_segredos.py`: it **serializes each record with the production
formatter** (`FormatadorJSON`, the same one `configurar_logging` installs) and asserts the secret
appears in no line. Testing only `caplog.text` would miss a secret hidden in `extra_campos`, which
is exactly where it would get in.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import motor, service
from cflow_core.ingestao import repositorio as repo
from cflow_core.ingestao.armazenamento import ArmazenamentoLocal
from cflow_core.ingestao.conectores import Payload, Requisicao
from cflow_core.ingestao.contrato import Fatia
from cflow_core.logging import FormatadorJSON

SEGREDO = "SECRET-XYZ"
URL = f"https://data.example.com/series.csv?apikey={SEGREDO}"
CSV = b"COD;VALOR\nA;1,50\nB;2,50\n"

FATIA = Fatia(chave="2026-08", inicio=date(2026, 8, 1), fim=date(2026, 8, 31))


def _contrato() -> dict:
    """Contract with the access key IN THE QUERYSTRING - the real `rest_api`/`odata` arrangement."""
    return {
        "classe": "http_file",
        "dataset": "example_test",
        "janela": {"tipo": "mensal"},
        "acesso": {"url_padrao": URL, "variaveis": {"apikey": SEGREDO}},
        "formato": {"tipo": "csv", "encoding": "utf-8", "delimitador": ";"},
    }


def _fonte() -> dict:
    return {
        "id": uuid.uuid4(),
        "nome": "Example source",
        "dataset": "example_test",
        "contrato": _contrato(),
    }


class _ConectorFalso:
    """Discovers one request and returns whatever the test dictates (bytes or exception)."""

    classe = "http_file"

    def __init__(self, resultado) -> None:
        self._resultado = resultado

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]:
        return [Requisicao(fatia=fatia, url=URL, nome_arquivo="serie.csv")]

    async def baixar(self, contrato, req, *, etag=None, ultima_modificacao=None) -> Payload:
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return Payload(dados=self._resultado, origem_url=URL, tipo_mime="text/csv")


@pytest.fixture
def sem_banco(monkeypatch):
    """Neutralizes the repository: this is a UNIT test, it does not touch Postgres.

    Returns what was "written", to prove the log describes the same outcome as the table.
    """
    gravado: dict[str, list] = {"quarentena": [], "fatias": [], "lotes": []}

    async def _ultimo_lote(*a, **k):
        return None

    async def _lote_por_hash(*a, **k):
        return None

    async def _registrar_lote(**k):
        gravado["lotes"].append(k)
        return uuid.uuid4()

    async def _concluir_lote(*a, **k):
        return None

    async def _registrar_quarentena(**k):
        gravado["quarentena"].append(k)

    async def _marcar_lote_quarentena(*a, **k):
        return None

    async def _registrar_fatia(**k):
        gravado["fatias"].append(k)

    monkeypatch.setattr(repo, "ultimo_lote_da_fatia", _ultimo_lote)
    monkeypatch.setattr(repo, "lote_por_hash", _lote_por_hash)
    monkeypatch.setattr(repo, "registrar_lote", _registrar_lote)
    monkeypatch.setattr(repo, "concluir_lote", _concluir_lote)
    monkeypatch.setattr(repo, "registrar_quarentena", _registrar_quarentena)
    monkeypatch.setattr(repo, "marcar_lote_quarentena", _marcar_lote_quarentena)
    monkeypatch.setattr(repo, "registrar_fatia", _registrar_fatia)
    return gravado


def _linhas(caplog) -> list[str]:
    """Each record serialized by the PRODUCTION formatter, not by `caplog.text`.

    It is the only way to see `extra_campos`: that is where a secret would get in, and
    `caplog.text` shows only the message (the event name), which never carries a value."""
    fmt = FormatadorJSON()
    return [fmt.format(r) for r in caplog.records]


def _eventos(caplog, nome: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == nome]


# --- Happy path -----------------------------------------------------------------------


async def test_fatia_ingerida_registra_desfecho_linhas_e_bytes(
    tmp_path, monkeypatch, caplog, sem_banco
) -> None:
    """A slice that lands in bronze emits `ingestao_fatia_concluida` with what it moved.

    `linhas`/`bytes` come from the metrics dict the engine fills in - and not from `contagens`,
    which is persisted and crosses Temporal: the log must not change the stored data."""
    caplog.set_level(logging.DEBUG, logger="cflow.ingestao")
    monkeypatch.setattr(motor, "get_conector", lambda classe: _ConectorFalso(CSV))
    fonte = _fonte()
    lake = ArmazenamentoLocal(raiz=tmp_path)
    metricas: dict = {}

    async def _corpo() -> dict:
        return await motor.processar_fatia(
            fonte=fonte,
            execucao_id=None,
            fatia=FATIA,
            lake=lake,
            hoje=date(2026, 9, 1),
            metricas=metricas,
        )

    contagens = await service._com_log(
        fonte_id=fonte["id"],
        execucao_id=uuid.uuid4(),
        fatia_ctx={"chave": FATIA.chave, "inicio": "2026-08-01", "fim": "2026-08-31"},
        corpo=_corpo,
        dataset=fonte["dataset"],
        metricas=metricas,
        tentativa=1,
    )

    assert contagens == {"novo": 1}
    (evento,) = _eventos(caplog, "ingestao_fatia_concluida")
    campos = evento.extra_campos
    assert evento.levelno == logging.INFO
    assert campos["desfecho"] == "novo"
    assert campos["dataset"] == "example_test"
    assert campos["chave_lote"] == "2026-08"
    assert campos["linhas"] == 2 and campos["bytes"] == len(CSV)
    assert campos["tentativa"] == 1 and campos["duracao_ms"] >= 0
    # The log describes the SAME outcome as the table: diverging here is what makes nobody trust the log.
    assert sem_banco["fatias"][0]["chave_lote"] == campos["chave_lote"]
    assert all(SEGREDO not in linha for linha in _linhas(caplog))


# --- Error: the secret in the querystring ------------------------------------------------------


async def test_quarentena_nao_vaza_a_chave_de_acesso_da_url(
    tmp_path, monkeypatch, caplog, sem_banco
) -> None:
    """A DATA failure (becomes quarantine) whose message quotes the whole URL, with `?apikey=`.

    This is the real case: `corpo_ou_erro` builds "The source returned an empty body (<url>)."
    with the raw URL. The event must come out with host and path - which answer "where did this
    come from?" - and without the query.
    """
    caplog.set_level(logging.DEBUG, logger="cflow.ingestao")
    falha = ErroAplicacao(
        codigo="download_vazio",
        mensagem=f"The source returned an empty body ({URL}).",
        status=502,
    )
    monkeypatch.setattr(motor, "get_conector", lambda classe: _ConectorFalso(falha))
    fonte = _fonte()

    contagens = await motor.processar_fatia(
        fonte=fonte,
        execucao_id=None,
        fatia=FATIA,
        lake=ArmazenamentoLocal(raiz=tmp_path),
        hoje=date(2026, 9, 1),
    )

    assert contagens == {"quarentena": 1}
    (evento,) = _eventos(caplog, "ingestao_quarentena")
    campos = evento.extra_campos
    assert evento.levelno == logging.WARNING
    assert campos["etapa"] == "download" and campos["codigo"] == "download_vazio"
    assert campos["dataset"] == "example_test" and campos["chave_lote"] == "2026-08"
    # Host and path stay; the querystring (where the key lives) goes away.
    assert "data.example.com/series.csv" in campos["motivo"]
    assert "apikey" not in campos["motivo"]
    assert caplog.records, "with no records at all the test would pass vacuously"
    assert all(SEGREDO not in linha for linha in _linhas(caplog))


async def test_falha_de_infra_e_avisada_e_reerguida_sem_segredo(caplog, sem_banco) -> None:
    """An INFRA failure bubbles up (Temporal needs to see it to retry) - but leaves a trace first.

    Without this line, a slice that failed three times vanishes from the log and only exists as
    `desfecho = falha` in a table nobody queries while the incident is happening."""
    caplog.set_level(logging.DEBUG, logger="cflow.ingestao")
    fonte = _fonte()

    async def _corpo() -> dict:
        raise ErroAplicacao(
            codigo="falha_download",
            mensagem=f"Failed to access {URL}: host unreachable.",
            status=502,
        )

    with pytest.raises(ErroAplicacao):
        await service._com_log(
            fonte_id=fonte["id"],
            execucao_id=uuid.uuid4(),
            fatia_ctx={"chave": FATIA.chave, "inicio": "2026-08-01", "fim": "2026-08-31"},
            corpo=_corpo,
            dataset=fonte["dataset"],
            tentativa=1,
        )

    (evento,) = _eventos(caplog, "ingestao_fatia_falhou")
    assert evento.levelno == logging.WARNING
    assert evento.extra_campos["codigo"] == "falha_download"
    assert "apikey" not in evento.extra_campos["motivo"]
    # The slice is written BEFORE re-raising: log and table tell the same story.
    assert sem_banco["fatias"][0]["desfecho"] == "falha"
    assert all(SEGREDO not in linha for linha in _linhas(caplog))


# --- The event name is the filter key -------------------------------------------------


async def test_nome_do_evento_sobrevive_ao_formatador(
    tmp_path, monkeypatch, caplog, sem_banco
) -> None:
    """`extra_campos` OVERWRITES the `FormatadorJSON` fields - and the event name lives in
    `mensagem`.

    A `mensagem` field in `extra_campos` erased the event name from the serialized line, i.e. it
    erased exactly the key the log metric filters on: the line kept coming out, with all the
    data, and the alert never fired. That is why the error text is called `motivo`.
    """
    caplog.set_level(logging.DEBUG, logger="cflow.ingestao")
    falha = ErroAplicacao(codigo="download_vazio", mensagem=f"Empty body ({URL}).", status=502)
    monkeypatch.setattr(motor, "get_conector", lambda classe: _ConectorFalso(falha))

    await motor.processar_fatia(
        fonte=_fonte(),
        execucao_id=None,
        fatia=FATIA,
        lake=ArmazenamentoLocal(raiz=tmp_path),
        hoje=date(2026, 9, 1),
    )

    linha = json.loads(_linhas(caplog)[0])
    assert linha["mensagem"] == "ingestao_quarentena"
    assert linha["logger"] == "cflow.ingestao.motor" and linha["nivel"] == "WARNING"
    assert linha["motivo"].startswith("Empty body (https://data.example.com/series.csv")
    assert SEGREDO not in json.dumps(linha, ensure_ascii=False)


# --- URL sanitization -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        (f"https://h.example.com/a/b.csv?apikey={SEGREDO}", "https://h.example.com/a/b.csv"),
        # userinfo is a credential just like the query - it goes too.
        (f"https://user:{SEGREDO}@h.example.com/a", "https://h.example.com/a"),
        ("https://h.example.com:8443/a?x=1", "https://h.example.com:8443/a"),
        ("not a url", ""),
    ],
)
def test_origem_sem_query(entrada: str, esperado: str) -> None:
    assert motor.origem_sem_query(entrada) == esperado


def test_sanear_texto_preserva_o_que_nao_e_url() -> None:
    """Sanitizing must not eat the message: it is an operations tool."""
    assert motor.sanear_texto("line 5,907 of the file has 8 fields") == (
        "line 5,907 of the file has 8 fields"
    )
    assert motor.sanear_texto(None) == ""
