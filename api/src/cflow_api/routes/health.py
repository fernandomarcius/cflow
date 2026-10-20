"""Health check and readiness.

Two routes, deliberately aimed at different audiences:

- **`GET /saude` (liveness)**: the API is up and Postgres answers. Deploy pipelines grep the
  response (`"status":"ok"`), so its format is a **contract**: fields may be added, none may be
  renamed or removed. `GET /healthz` is the same route under the conventional Kubernetes-style
  probe name.
- **`GET /saude/pronto` (readiness)**: the API can actually WORK: database, lake and Temporal.
  Kept separate because the three dependencies fail in different ways, and a probe that confuses
  "up" with "able to serve" either restarts the container needlessly or keeps alive a process that
  executes nothing. This one touches the network (lake and Temporal); liveness never does.

`momento` is converted to the display timezone at the edge. When the database is unavailable,
both respond 503 with the standard error envelope.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter
from sqlalchemy import text

from cflow_core.config import get_settings
from cflow_core.db import transacao, verificar_conexao
from cflow_core.errors import DetalheErro, ErroAplicacao
from cflow_core.timezone import agora_utc, para_exibicao

router = APIRouter()

# Maximum time for EACH readiness check. The probe is called every few seconds: a slow dependency
# must answer "not ready" quickly instead of holding the uvicorn worker.
TIMEOUT_PRONTO = 5.0

# Reuse window for the lake verdict. A naive readiness check makes, ON EVERY PROBE, one request
# per bucket to object storage: at a 5 s interval that is tens of thousands of calls per day PER
# REPLICA, always to get the same answer. Failures are cached too: hammering the origin while the
# bucket is down is exactly what does not help.
#
# The price is explicit: a lake outage can take up to `TTL_CACHE_LAKE` longer to show up in the
# probe. It is worth paying because the API **does not depend on the lake to serve most requests**
# (login, Studio, model queries); the worker is what actually reads and writes the lake. If the
# probe ever needs to react faster, lower the TTL rather than removing it.
TTL_CACHE_LAKE = 30.0
_cache_lake: tuple[float, dict] | None = None

# The Temporal client is deliberately reused across probes: `Client.connect` opens a gRPC channel
# and this SDK version does not expose `close()`, so connecting per probe leaks one channel per
# call. Kept here, it is ONE channel per process; when `check_health` fails the client is dropped
# and the next probe reconnects.
_cliente_temporal = None


def limpar_cache_readiness() -> None:
    """Forget the cached verdict (lake credential rotation, and tests)."""
    global _cache_lake, _cliente_temporal
    _cache_lake = None
    _cliente_temporal = None


def versao_no_ar() -> str:
    """Short SHA of the running image, or 'desconhecida' (unknown).

    Without it there is no way to know which build is live without logging into the host and
    inspecting the container, and "is the version with the fix live?" is the first question of
    every incident. The variable is injected at build/deploy time; the fallback is honest instead
    of inventing a number.
    """
    for chave in ("CFLOW_VERSAO", "GIT_SHA", "GITHUB_SHA"):
        valor = (os.environ.get(chave) or "").strip()
        if valor:
            return valor[:12]
    return "desconhecida"


# `/healthz` is the conventional fixed probe path (readiness and liveness on the service port).
# Same semantics as `/saude`: 200 only with the database answering, 503 in the standard envelope
# without it.
@router.get("/healthz", include_in_schema=False)
@router.get("/saude")
async def saude() -> dict:
    try:
        banco_ok = await verificar_conexao()
    except Exception as exc:  # noqa: BLE001 - becomes a 503 in the standard envelope
        raise ErroAplicacao(
            codigo="servico_indisponivel",
            mensagem="Metadata database is currently unavailable.",
            status=503,
        ) from exc

    if not banco_ok:
        raise ErroAplicacao(
            codigo="servico_indisponivel",
            mensagem="Metadata database is currently unavailable.",
            status=503,
        )

    return {
        "status": "ok",
        "banco": "ok",
        "versao": versao_no_ar(),
        "momento": para_exibicao(agora_utc()).isoformat(),
    }


async def _checar_banco() -> dict:
    ok = await verificar_conexao()
    return {"ok": bool(ok)} if ok else {"ok": False, "causa": "SELECT 1 did not respond"}


async def _checar_lake() -> dict:
    """Test the lake backend ACTUALLY in use: the destination of the active sources.

    There is no single "lake connection" variable: the destination is declared per source
    (`ingestao_fontes.destino_conexao_id`), and with no destination the backend is the local
    `CFLOW_LAKE_DIR` (air-gapped default). Testing `abrir(None)` would always report "disk ok" in
    an environment that writes to object storage, and a green readiness with an unreachable bucket
    is worse than no readiness at all.

    The object-storage `testar` already treats a 403 from an object-only permission as an accepted
    credential (failing there would fail the correct production setup), so nothing is
    reimplemented here.
    """
    global _cache_lake

    if _cache_lake is not None and time.monotonic() - _cache_lake[0] < TTL_CACHE_LAKE:
        return _cache_lake[1]

    from cflow_core.ingestao.armazenamento import abrir

    async with transacao() as conn:
        destinos = [
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT destino_conexao_id FROM ingestao_fontes WHERE ativo = true"
                    )
                )
            ).all()
        ]
    if not destinos:
        destinos = [None]  # no active source: all that is left to prove is the default backend

    detalhes: list[dict] = []
    for conexao_id in destinos:
        try:
            resultado = await (await abrir(conexao_id)).testar()
        except ErroAplicacao as e:
            resultado = {"ok": False, "causa": e.mensagem}
        except Exception as e:  # noqa: BLE001 - sanitised cause; readiness returns a verdict
            resultado = {"ok": False, "causa": type(e).__name__}
        detalhes.append({"conexao_id": str(conexao_id) if conexao_id else None, **resultado})

    ruim = next((d for d in detalhes if not d.get("ok")), None)
    veredito = {
        "ok": ruim is None,
        "causa": ruim.get("causa") if ruim else None,
        "destinos": detalhes,
    }
    _cache_lake = (time.monotonic(), veredito)
    return veredito


async def _checar_temporal() -> dict:
    """Is Temporal reachable? With `CFLOW_USAR_TEMPORAL=false` there is nothing to reach (inline
    execution)."""
    global _cliente_temporal

    cfg = get_settings()
    if not cfg.usar_temporal:
        return {"ok": True, "detalhe": "disabled (inline execution)"}
    from temporalio.client import Client

    try:
        if _cliente_temporal is None:
            _cliente_temporal = await Client.connect(cfg.temporal_host)
        # `connect` opens the channel; `check_health` is Temporal's own gRPC health service, which
        # separates "the port accepted TCP" from "the service responds".
        saudavel = await _cliente_temporal.service_client.check_health()
    except Exception as e:  # noqa: BLE001 - host/port in the cause, never a credential
        _cliente_temporal = None  # channel possibly dead: the next probe reconnects
        return {"ok": False, "causa": f"{type(e).__name__}: {cfg.temporal_host} unreachable"}
    if not saudavel:
        return {"ok": False, "causa": f"{cfg.temporal_host} responded but is not healthy"}
    return {"ok": True, "detalhe": cfg.temporal_host}


async def _com_timeout(nome: str, corrotina) -> tuple[str, dict]:
    try:
        return nome, await asyncio.wait_for(corrotina, timeout=TIMEOUT_PRONTO)
    except TimeoutError:
        return nome, {"ok": False, "causa": f"did not respond within {TIMEOUT_PRONTO:.0f}s"}
    except Exception as e:  # noqa: BLE001 - readiness returns a verdict, not a stack trace
        return nome, {"ok": False, "causa": type(e).__name__}


@router.get("/saude/pronto")
async def pronto() -> dict:
    """Readiness: database + lake + Temporal. 200 when all three answer; 503 naming the failures.

    Open like `/saude` (no authentication): the caller is the orchestrator, which has no session.
    That is why the response carries a verdict and cause, never a URL with a query string, a
    credential or a stack trace.
    """
    checagens = await asyncio.gather(
        _com_timeout("banco", _checar_banco()),
        _com_timeout("lake", _checar_lake()),
        _com_timeout("temporal", _checar_temporal()),
    )
    resultado = dict(checagens)
    falhas = [nome for nome, r in resultado.items() if not r.get("ok")]
    if falhas:
        raise ErroAplicacao(
            codigo="nao_pronto",
            mensagem="The API is up, but a dependency did not respond.",
            status=503,
            detalhes=[
                DetalheErro(campo=nome, mensagem=resultado[nome].get("causa") or "unavailable")
                for nome in falhas
            ],
        )
    return {
        "status": "pronto",
        "versao": versao_no_ar(),
        "momento": para_exibicao(agora_utc()).isoformat(),
        "checagens": resultado,
    }
