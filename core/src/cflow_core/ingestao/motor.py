"""Ingestion engine: processes ONE slice end to end (discover -> landing -> bronze).

It is the retry unit (one Temporal activity). Everything here is designed to be safely re-runnable:
idempotency is by **payload hash** against the unique index `(fonte, chave_lote, sha256)`, so
re-running the same slice with the same bytes is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime
from urllib.parse import urlparse

from ..errors import ErroAplicacao
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao
from . import repositorio as repo
from .armazenamento import Armazenamento
from .bronze import ErroNormalizacao, normalizar
from .conectores import Requisicao, get_conector
from .contrato import Fatia, hash_contrato

MANIFESTO = "_manifesto.json"

# The event name is a CONTRACT (it becomes the log-metric filter key), not free text.
logger = logging.getLogger("cflow.ingestao.motor")

# URL embedded in an error message. Stops at whitespace and common closing punctuation so that
# "empty body (https://x/y?k=v)." does not drag the ")." into the URL.
_URL_NO_TEXTO = re.compile(r"""[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)\]}"'<>]+""")


def origem_sem_query(url: str) -> str:
    """`scheme://host[:port]/path` - **never** the querystring or the userinfo.

    The `rest_api` and `odata` connectors put access keys in `params`, and logs get copied and
    exported out of the platform far more easily than the audit trail. Host and path already answer
    "where did this come from?"; the query only adds risk."""
    try:
        p = urlparse(url)
        porta = f":{p.port}" if p.port else ""
    except ValueError:
        return ""
    if not p.scheme or not p.hostname:
        return ""
    return f"{p.scheme}://{p.hostname}{porta}{p.path}"


def sanear_texto(texto: str | None) -> str:
    """Error message ready for the log: every embedded URL loses its querystring.

    The message comes from the connector and from `ErroNormalizacao`, and at least one of them quotes
    the full URL (`download_vazio`). Without this, a source whose key travels in `?apikey=` would
    publish the credential in a log line for every empty file from the origin."""
    return _URL_NO_TEXTO.sub(lambda m: origem_sem_query(m.group(0)) or "[url]", texto or "")


def _medir(metricas: dict | None, *, linhas: int = 0, bytes_: int = 0) -> None:
    """Accumulates what the slice moved, for whoever LOGS the slice (the service).

    Passed as a parameter rather than added to `contagens`, because `contagens` is persisted in
    `ingestao_fatias.contagens` and crosses the Temporal workflow: stuffing metrics there would
    change the stored data just to emit a log line."""
    if metricas is None:
        return
    metricas["linhas"] = metricas.get("linhas", 0) + linhas
    metricas["bytes"] = metricas.get("bytes", 0) + bytes_


# --- Lake paths ---------------------------------------------------------------------


def _particao(dataset: str, ingestion_date: date, chave: str, sha12: str) -> str:
    """`<dataset>/ingestion_date=YYYY-MM-DD/<fatia>/<sha12>/`.

    The hash in the path lets two versions of the SAME slice (the origin republishes the current
    month) coexist without overwriting - landing is immutable: fixing means adding, never replacing."""
    return f"{dataset}/ingestion_date={ingestion_date.isoformat()}/{chave}/{sha12}"


def caminho_landing(dataset: str, ingestion_date: date, chave: str, sha12: str, nome: str) -> str:
    return f"{_particao(dataset, ingestion_date, chave, sha12)}/{nome}"


def caminho_bronze(dataset: str, ingestion_date: date, chave: str, sha12: str) -> str:
    return f"{_particao(dataset, ingestion_date, chave, sha12)}/dados.parquet"


def caminho_quarentena(
    dataset: str, ingestion_date: date, chave: str, etapa: str, nome: str
) -> str:
    return f"{dataset}/ingestion_date={ingestion_date.isoformat()}/{chave}/{etapa}/{nome}"


# --- Manifest ----------------------------------------------------------------------------


def montar_manifesto(
    *,
    contrato: dict,
    fatia: Fatia,
    sha256: str,
    bytes_: int,
    origem_url: str,
    etag: str | None,
    ultima_modificacao: str | None,
    landing_caminho: str,
    execucao_id: str | None,
    momento: datetime | None = None,
    extra: dict | None = None,
) -> dict:
    """The manifest is the batch's provenance proof: which bytes, from where, when, under which
    contract. It travels with the partition - whoever reads the Parquet without Postgres access can
    still answer "where did this number come from?"."""
    return {
        "dataset": contrato["dataset"],
        "classe": contrato["classe"],
        "chave_lote": fatia.chave,
        "janela": {"inicio": fatia.inicio.isoformat(), "fim": fatia.fim.isoformat()},
        "sha256": sha256,
        "bytes": bytes_,
        "origem_url": origem_url,
        "etag": etag,
        "ultima_modificacao": ultima_modificacao,
        "encoding": (contrato.get("formato") or {}).get("encoding"),
        "contrato_hash": hash_contrato(contrato),
        "landing_caminho": landing_caminho,
        "execucao_id": execucao_id,
        "momento": para_exibicao(momento or agora_utc(), FUSO_SAO_PAULO).isoformat(),
        **(extra or {}),
    }


def _bytes_json(d: dict) -> bytes:
    return json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")


# --- Processing one slice -------------------------------------------------------------


async def _gravar_se_novo(
    lake: Armazenamento, camada: str, caminho: str, dados: bytes, mime: str
) -> str:
    """Writes while respecting immutability: an object that already exists is a no-op (not an error).

    This really happens when the worker dies between writing the object and committing the batch:
    the activity redelivery finds the same path with the same bytes (the path has the hash), and
    failing here would leave the slice stuck forever."""
    try:
        return await lake.gravar(camada, caminho, dados, tipo_mime=mime)
    except ErroAplicacao as e:
        if e.codigo == "objeto_ja_existe":
            return lake.uri(camada, caminho)
        raise


async def _quarentenar(
    *,
    lake: Armazenamento,
    fonte: dict,
    execucao_id: uuid.UUID | None,
    fatia: Fatia,
    etapa: str,
    codigo: str,
    mensagem: str,
    dados: bytes | None,
    nome: str,
    sha256: str | None,
    hoje: date,
    lote_id: uuid.UUID | None = None,
) -> None:
    """DLQ: the raw payload goes to the quarantine bucket and the failure becomes a queryable record.

    Without this, a malformed file either vanishes (the worker logs and moves on) or brings down the
    whole window - and in both cases the missing data has no name or address."""
    uri = None
    if dados:
        try:
            uri = await _gravar_se_novo(
                lake,
                "quarantine",
                caminho_quarentena(fonte["dataset"], hoje, fatia.chave, etapa, nome),
                dados,
                "application/octet-stream",
            )
        except ErroAplicacao:
            uri = None  # failing to store the payload must not erase the failure RECORD
    await repo.registrar_quarentena(
        fonte_id=fonte["id"],
        execucao_id=execucao_id,
        chave_lote=fatia.chave,
        etapa=etapa,
        codigo=codigo,
        # Sanitized in what is persisted too, not only in the log: `conectores/base.py` builds the
        # message with the origin URL, and `rest_api`/`odata` carry access keys in `params`. This
        # column is shown in the Bronze drill-down - unsanitized, the credential would travel from a
        # table nobody read to the browser screen.
        mensagem=sanear_texto(mensagem),
        uri=uri,
        sha256=sha256,
    )
    if lote_id is not None:
        await repo.marcar_lote_quarentena(lote_id)
    logger.warning(
        "ingestao_quarentena",
        extra={
            "extra_campos": {
                "fonte": str(fonte["id"]),
                "dataset": fonte["dataset"],
                "chave_lote": fatia.chave,
                "etapa": etapa,
                "codigo": codigo,
                # `motivo`, NOT `mensagem`: the formatter puts the event name in `mensagem` and
                # `extra_campos` overwrites whatever is there - the line would lose the event name,
                # which is exactly the key the log metric filters on. (`motivo` is also the name the
                # execution screen uses to join error and quarantine.)
                # Truncated: a parse message may carry a snippet of the bad row, and a log line with
                # 4 KB of CSV in it is what makes nobody read the log.
                "motivo": sanear_texto(mensagem)[:200],
                "uri": uri,
                "sha256": sha256,
                "bytes": len(dados) if dados else 0,
            }
        },
    )


async def _processar_payload(
    *,
    lake: Armazenamento,
    fonte: dict,
    contrato: dict,
    execucao_id: uuid.UUID | None,
    fatia: Fatia,
    dados: bytes,
    nome_arquivo: str,
    origem_url: str,
    etag: str | None,
    ultima_modificacao: str | None,
    mime: str,
    extra: dict | None,
    hoje: date,
    metricas: dict | None = None,
) -> str:
    """Landing (immutable) -> batch record -> bronze. Returns the slice outcome."""
    sha256 = hashlib.sha256(dados).hexdigest()
    sha12 = sha256[:12]
    dataset = fonte["dataset"]

    # Idempotency: same bytes for the same slice = nothing to do. Cheap (one query) and decided
    # BEFORE writing; the unique index is the guard against races.
    #
    # But "already ingested" only holds FOR THIS DESTINATION: switching the lake connection (disk ->
    # bucket, or bucket -> bucket) would find the same batches registered and write nothing to the
    # new destination - the migration would end "successfully" with an empty bucket.
    ja = await repo.lote_por_hash(fonte["id"], fatia.chave, sha256)
    ingestion_date = ja["ingestion_date"] if ja else hoje
    caminho = caminho_landing(dataset, ingestion_date, fatia.chave, sha12, nome_arquivo)
    if ja and ja["status"] == "bronze" and ja["landing_uri"] == lake.uri("landing", caminho):
        # A reused batch still DOWNLOADED the bytes (the hash matched, not the conditional GET):
        # counting them separates "spent no network" from "spent it and discarded".
        _medir(metricas, bytes_=len(dados))
        return "reusado"

    manifesto = montar_manifesto(
        contrato=contrato,
        fatia=fatia,
        sha256=sha256,
        bytes_=len(dados),
        origem_url=origem_url,
        etag=etag,
        ultima_modificacao=ultima_modificacao,
        landing_caminho=caminho,
        execucao_id=str(execucao_id) if execucao_id else None,
        extra=extra,
    )
    landing_uri = await _gravar_se_novo(lake, "landing", caminho, dados, mime)
    await _gravar_se_novo(
        lake,
        "landing",
        f"{_particao(dataset, ingestion_date, fatia.chave, sha12)}/{MANIFESTO}",
        _bytes_json(manifesto),
        "application/json",
    )

    lote_id = (
        ja["id"]
        if ja
        else await repo.registrar_lote(
            fonte_id=fonte["id"],
            execucao_id=execucao_id,
            chave_lote=fatia.chave,
            sha256=sha256,
            bytes_=len(dados),
            etag=etag,
            ultima_modificacao=ultima_modificacao,
            origem_url=origem_url,
            landing_uri=landing_uri,
            ingestion_date=ingestion_date,
            manifesto=manifesto,
        )
    )
    if lote_id is None:  # another worker registered between the query and the INSERT
        return "reusado"

    # `diagnostico` collects what the invalid-row tolerance discarded (per-source contract). It goes
    # into the manifest: tolerating without recording would be the silent discard that quarantine
    # exists to prevent.
    diagnostico: dict = {}
    try:
        parquet, linhas, colunas = await normalizar(dados, contrato, diagnostico=diagnostico)
    except ErroNormalizacao as e:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=execucao_id,
            fatia=fatia,
            etapa=e.etapa,
            codigo=e.codigo,
            mensagem=e.mensagem,
            dados=dados,
            nome=nome_arquivo,
            sha256=sha256,
            hoje=ingestion_date,
            lote_id=lote_id,
        )
        _medir(metricas, bytes_=len(dados))
        return "quarentena"

    bronze_caminho = caminho_bronze(dataset, ingestion_date, fatia.chave, sha12)
    bronze_uri = await _gravar_se_novo(
        lake, "bronze", bronze_caminho, parquet, "application/vnd.apache.parquet"
    )
    manifesto_final = {
        **manifesto,
        "linhas": linhas,
        "colunas": colunas,
        "bronze_caminho": bronze_caminho,
        "bronze_bytes": len(parquet),
        **diagnostico,
    }
    await _gravar_se_novo(
        lake,
        "bronze",
        f"{_particao(dataset, ingestion_date, fatia.chave, sha12)}/{MANIFESTO}",
        _bytes_json(manifesto_final),
        "application/json",
    )
    await repo.concluir_lote(
        lote_id,
        bronze_uri=bronze_uri,
        linhas=linhas,
        manifesto=manifesto_final,
        landing_uri=landing_uri,
    )
    _medir(metricas, linhas=linhas, bytes_=len(dados))
    return "novo"


def _somar(contagens: dict, desfecho: str) -> None:
    contagens[desfecho] = contagens.get(desfecho, 0) + 1


async def processar_fatia(
    *,
    fonte: dict,
    execucao_id: uuid.UUID | str | None,
    fatia: Fatia,
    lake: Armazenamento,
    hoje: date | None = None,
    metricas: dict | None = None,
) -> dict:
    """Downloads and processes the slice. NEVER raises for a data failure: a data failure becomes
    quarantine (the next slice continues). Raises only INFRA failures (lake down, database down) -
    which is what the Temporal retry fixes."""
    contrato = fonte["contrato"]
    conector = get_conector(contrato["classe"])
    exec_uuid = uuid.UUID(str(execucao_id)) if execucao_id else None
    hoje = hoje or para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()
    contagens: dict = {}

    try:
        requisicoes = await conector.descobrir(contrato, fatia)
    except ErroAplicacao as e:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=exec_uuid,
            fatia=fatia,
            etapa="descoberta",
            codigo=e.codigo,
            mensagem=e.mensagem,
            dados=None,
            nome="descoberta",
            sha256=None,
            hoje=hoje,
        )
        _somar(contagens, "quarentena")
        return contagens

    anterior = await repo.ultimo_lote_da_fatia(fonte["id"], fatia.chave)
    # The conditional GET asks "did the origin change?"; what matters when the DESTINATION changed is
    # "does it exist in this lake?". A batch landed in the old lake would return 304 and the migration
    # would end "no news" with the new bucket empty. A batch from another destination is no reference.
    if anterior and not (anterior.get("landing_uri") or "").startswith(lake.raiz_uri("landing")):
        anterior = None
    for req in requisicoes:
        await _uma_requisicao(
            conector=conector,
            contrato=contrato,
            fonte=fonte,
            execucao_id=exec_uuid,
            fatia=fatia,
            req=req,
            anterior=anterior,
            lake=lake,
            hoje=hoje,
            contagens=contagens,
            metricas=metricas,
        )
    return contagens


async def _uma_requisicao(
    *,
    conector,
    contrato,
    fonte,
    execucao_id,
    fatia,
    req: Requisicao,
    anterior,
    lake,
    hoje,
    contagens,
    metricas: dict | None = None,
) -> None:
    try:
        payload = await conector.baixar(
            contrato,
            req,
            etag=(anterior or {}).get("etag"),
            ultima_modificacao=(anterior or {}).get("ultima_modificacao"),
        )
    except ErroAplicacao as e:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=execucao_id,
            fatia=fatia,
            etapa="download",
            codigo=e.codigo,
            mensagem=e.mensagem,
            dados=None,
            nome=req.nome_arquivo,
            sha256=None,
            hoje=hoje,
        )
        _somar(contagens, "quarentena")
        return

    if payload.nao_publicado:
        # The origin does not have this slice. Counted and shown in the summary, but not a DLQ entry.
        _somar(contagens, "nao_publicado")
        return

    if payload.nao_modificado:
        # 304: the origin confirmed the slice is unchanged. Neither a new batch nor a failure -
        # exactly what incremental loading exists to achieve.
        _somar(contagens, "nao_modificado")
        return

    desfecho = await _processar_payload(
        lake=lake,
        fonte=fonte,
        contrato=contrato,
        execucao_id=execucao_id,
        fatia=fatia,
        dados=payload.dados,
        nome_arquivo=req.nome_arquivo,
        origem_url=payload.origem_url,
        etag=payload.etag,
        ultima_modificacao=payload.ultima_modificacao,
        mime=payload.tipo_mime,
        extra=payload.detalhe or None,
        hoje=hoje,
        metricas=metricas,
    )
    _somar(contagens, desfecho)


async def reprocessar_fatia(
    *,
    fonte: dict,
    execucao_id: uuid.UUID | str | None,
    lote: dict,
    lake: Armazenamento,
    metricas: dict | None = None,
) -> dict:
    """REPLAY: rebuilds bronze from LANDING, without touching the network.

    This is what makes the immutable landing worthwhile: when the bronze rule changes (encoding,
    fixed-width layout, expected column), years of history are reprocessed without downloading
    anything again - and without depending on the origin still publishing the old file."""
    contrato = fonte["contrato"]
    exec_uuid = uuid.UUID(str(execucao_id)) if execucao_id else None
    manifesto = lote.get("manifesto") or {}
    caminho = manifesto.get("landing_caminho")
    # No window in the manifest (old batch): the ingestion day is the best honest guess.
    janela = manifesto.get("janela") or {}
    dia = lote["ingestion_date"].isoformat()
    fatia = Fatia(
        chave=lote["chave_lote"],
        inicio=date.fromisoformat(janela.get("inicio", dia)),
        fim=date.fromisoformat(janela.get("fim", dia)),
    )
    contagens: dict = {}
    if not caminho:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=exec_uuid,
            fatia=fatia,
            etapa="replay",
            codigo="landing_sem_caminho",
            mensagem="The batch does not record its landing path.",
            dados=None,
            nome="replay",
            sha256=lote.get("sha256"),
            hoje=lote["ingestion_date"],
        )
        _somar(contagens, "quarentena")
        return contagens

    try:
        dados = await lake.ler("landing", caminho)
    except ErroAplicacao as e:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=exec_uuid,
            fatia=fatia,
            etapa="replay",
            codigo=e.codigo,
            mensagem=e.mensagem,
            dados=None,
            nome="replay",
            sha256=lote.get("sha256"),
            hoje=lote["ingestion_date"],
        )
        _somar(contagens, "quarentena")
        return contagens

    try:
        parquet, linhas, colunas = await normalizar(dados, contrato)
    except ErroNormalizacao as e:
        await _quarentenar(
            lake=lake,
            fonte=fonte,
            execucao_id=exec_uuid,
            fatia=fatia,
            etapa=e.etapa,
            codigo=e.codigo,
            mensagem=e.mensagem,
            dados=dados,
            nome=caminho.rsplit("/", 1)[-1],
            sha256=lote.get("sha256"),
            hoje=lote["ingestion_date"],
            lote_id=lote["id"],
        )
        _somar(contagens, "quarentena")
        return contagens

    sha12 = lote["sha256"][:12]
    bronze_caminho = caminho_bronze(fonte["dataset"], lote["ingestion_date"], fatia.chave, sha12)
    bronze_uri = await _gravar_se_novo(
        lake, "bronze", bronze_caminho, parquet, "application/vnd.apache.parquet"
    )
    manifesto_final = {
        **manifesto,
        "linhas": linhas,
        "colunas": colunas,
        "bronze_caminho": bronze_caminho,
        "bronze_bytes": len(parquet),
        "contrato_hash": hash_contrato(contrato),
        "replay_em": para_exibicao(agora_utc(), FUSO_SAO_PAULO).isoformat(),
        "execucao_id": str(exec_uuid) if exec_uuid else None,
    }
    await _gravar_se_novo(
        lake,
        "bronze",
        f"{_particao(fonte['dataset'], lote['ingestion_date'], fatia.chave, sha12)}/{MANIFESTO}",
        _bytes_json(manifesto_final),
        "application/json",
    )
    await repo.concluir_lote(
        lote["id"], bronze_uri=bronze_uri, linhas=linhas, manifesto=manifesto_final
    )
    _medir(metricas, linhas=linhas, bytes_=len(dados))
    _somar(contagens, "novo")
    return contagens
