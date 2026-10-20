"""`http` effect node: pure REQUEST BUILDING (what preview and execution build) kept apart from the
EFFECT (the HTTP calls). The SAME payload/URL is built on both paths: preview shows it, execution
fires it.

- `montar_requisicoes(cfg, colunas_pred, linhas_pred, params, base_url)` - pure, no I/O: resolves
  the URL (connection base + path by SIMPLE CONCATENATION, never urljoin, otherwise `//host` in the
  base or in a placeholder would leak the token to another host), substitutes `{{col}}`
  placeholders (row; URL-encoded in the path, JSON-encoded in the body) and `:param` (pipeline),
  and builds the body per mode (unico/por_linha/lote). Used by preview (dry-run) and execution.
- `executar_http(config, senha, plano, ...)` - effect: fires the requests with a single
  AsyncClient, a concurrency semaphore, per-slot rate limiting and a timeout; returns the result
  per request (status/raw response/error). The auth token goes only in the header, NEVER in the
  result/snapshot.

Placeholders:
- `{{col}}` is explicit syntax and always substituted (missing col -> empty in the path, `null` in
  the body). In the body the value is JSON-encoded, so write the template WITHOUT quotes around the
  placeholder: `{"name": {{name}}}` -> `{"name": "jane"}`.
- `:param` is only substituted when the parameter exists (otherwise it stays literal): `:` collides
  with the JSON separator (`{"a":true}`), so blind injection would break the body.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from ..conexoes.conectores.api_rest import _causa, _extrair_array, _headers, _linhas
from ..conexoes.conectores.base import serial
from ..errors import ErroAplicacao

_COL = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_PARAM = re.compile(r":([a-zA-Z_]\w*)")


def montar_url(base: str, caminho: str) -> str:
    """Final URL = connection base + path by SIMPLE CONCATENATION. NEVER urljoin/httpx base_url: a
    `//host` would be read as a protocol-relative URL and leak the token to another host. The path
    arrives validated (starts with '/', never '//') and injected values are URL-encoded (a `/` in a
    value becomes `%2F`), so concatenation never introduces `//`."""
    return base.rstrip("/") + caminho


def _subst_caminho(caminho: str, linha: dict | None, params: dict) -> str:
    """Substitute placeholders in the PATH with URL-encoding (a value never becomes a path separator)."""

    def _col(m: re.Match) -> str:
        v = (linha or {}).get(m.group(1))
        return quote(str(v), safe="") if v is not None else ""

    def _par(m: re.Match) -> str:
        nome = m.group(1)
        if nome in params and params[nome] is not None:
            return quote(str(params[nome]), safe="")
        return m.group(0)

    return _PARAM.sub(_par, _COL.sub(_col, caminho))


def _subst_header(valor: str, linha: dict | None, params: dict) -> str:
    """Substitute placeholders in a header VALUE: like the path, but WITHOUT URL-encoding (the value
    is literal: a `Bearer <token>` must not be percent-encoded). Missing `{{col}}` -> empty;
    nonexistent `:param` -> stays literal (same rule as path/body)."""

    def _col(m: re.Match) -> str:
        v = (linha or {}).get(m.group(1))
        return str(v) if v is not None else ""

    def _par(m: re.Match) -> str:
        nome = m.group(1)
        if nome in params and params[nome] is not None:
            return str(params[nome])
        return m.group(0)

    return _PARAM.sub(_par, _COL.sub(_col, valor))


def _headers_extra(cfg: dict, linha: dict | None, params: dict) -> dict:
    """Resolve the node's dynamic headers (`headers_extra`) PER REQUEST. Accepts a list of
    `{nome, valor}` (canonical format, consistent with `corpo_campos`) or a `{nome: valor}` object.
    Each `valor` goes through `_subst_header` (`{{col}}`/`:param`, no URL-encoding). Empty when
    absent, so behaviour matches http nodes without the field. NEVER goes to the snapshot (only to
    the request): the resolved value may contain a token."""
    extra = cfg.get("headers_extra")
    if not extra:
        return {}
    itens: list[tuple[object, object]]
    if isinstance(extra, dict):
        itens = list(extra.items())
    else:
        itens = [(h.get("nome"), h.get("valor")) for h in extra if isinstance(h, dict)]
    out: dict = {}
    for nome, valor in itens:
        if nome and isinstance(nome, str):
            out[nome] = _subst_header(str(valor) if valor is not None else "", linha, params)
    return out


def _subst_corpo_template(template: str, linha: dict | None, params: dict) -> str:
    """Substitute placeholders in the BODY with JSON-encoding (the value becomes a valid JSON token)."""

    def _col(m: re.Match) -> str:
        return json.dumps((linha or {}).get(m.group(1)), ensure_ascii=False, default=str)

    def _par(m: re.Match) -> str:
        nome = m.group(1)
        if nome not in params:
            return m.group(0)
        return json.dumps(params[nome], ensure_ascii=False, default=str)

    return _PARAM.sub(_par, _COL.sub(_col, template))


def _parse_template(template: str, linha: dict | None, params: dict) -> object:
    texto = _subst_corpo_template(template, linha, params)
    try:
        return json.loads(texto)
    except (ValueError, TypeError) as exc:
        raise ErroAplicacao(
            codigo="http_corpo_invalido",
            mensagem="Body (template) is not valid JSON after substituting the placeholders.",
            status=400,
        ) from exc


def _objeto_colunas(cfg: dict, linha: dict) -> dict:
    """JSON-safe object of the row: `corpo_campos` (from -> to) or all same-name columns."""
    campos = cfg.get("corpo_campos")
    if campos:
        return {c["para"]: serial(linha.get(c["de"])) for c in campos}
    return {k: serial(v) for k, v in linha.items()}


def _coluna_json(cfg: dict, linha: dict | None) -> object:
    """`corpo_tipo="coluna_json"`: send the value of ONE column (`corpo_coluna`) as the JSON body
    VERBATIM, without re-encoding. `str` -> `json.loads` (explicit failure if not valid JSON);
    `dict`/`list` (e.g. Postgres jsonb) -> used as is; missing/None -> None."""
    v = (linha or {}).get(cfg.get("corpo_coluna"))
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError) as exc:
            raise ErroAplicacao(
                codigo="http_corpo_invalido",
                mensagem="Body (coluna_json): the column value is not valid JSON.",
                status=400,
            ) from exc
    return v


def _corpo(cfg: dict, linha: dict | None, params: dict, *, unico: bool) -> object | None:
    """Body of ONE request (unico/por_linha). `template` -> JSON parsed from the template;
    `coluna_json` -> raw JSON of column `corpo_coluna`; `colunas` -> the row object (unico has no
    row, so no column body)."""
    tipo = cfg.get("corpo_tipo", "colunas")
    if tipo == "template":
        tmpl = cfg.get("corpo")
        return _parse_template(tmpl, linha, params) if tmpl else None
    if unico or linha is None:
        return None
    if tipo == "coluna_json":
        return _coluna_json(cfg, linha)
    return _objeto_colunas(cfg, linha)


def _corpo_lote(cfg: dict, chunk: list[dict], params: dict) -> object:
    """Body of batch mode.

    By default a JSON array of the chunk (one object per row). Some APIs take ONE object holding an
    inner list (e.g. `{items:[...], currency, ...}`): there, `lote_mesclar_campo` turns the batch
    into **a single object**: it takes the 1st body as the base and CONCATENATES into it the list of
    that field from all bodies in the chunk. The remaining scalar fields (dates, flags) come from
    the 1st body, so the chunk MUST be homogeneous in those fields (group by them when feeding the
    node)."""
    tipo = cfg.get("corpo_tipo", "colunas")
    if tipo == "template":
        tmpl = cfg.get("corpo")
        corpos: list = [_parse_template(tmpl, linha, params) for linha in chunk] if tmpl else []
    elif tipo == "coluna_json":
        corpos = [_coluna_json(cfg, linha) for linha in chunk]
    else:
        corpos = [_objeto_colunas(cfg, linha) for linha in chunk]
    campo = cfg.get("lote_mesclar_campo")
    if campo and corpos:
        base = dict(corpos[0]) if isinstance(corpos[0], dict) else {}
        itens: list = []
        for c in corpos:
            v = (c or {}).get(campo) if isinstance(c, dict) else None
            if isinstance(v, list):
                itens.extend(v)
            elif v is not None:
                itens.append(v)
        base[campo] = itens
        return base
    return corpos


@dataclass
class Requisicao:
    metodo: str
    url: str  # final URL (base + resolved path) - what preview shows and execution calls
    corpo: object | None  # dict/list/None - becomes the JSON body
    indice: int  # stable order (por_linha: row index; lote: chunk index; unico: 0)
    linha: dict | None = None  # por_linha: the source row (carried into the snapshot)
    linhas_enviadas: int | None = None  # lote: number of rows in the chunk
    # Dynamic headers ALREADY resolved (`headers_extra`, {{col}}/:param per request). Merged over
    # the connection's static headers at dispatch. They may contain a token, so they NEVER reach
    # the snapshot (linha_snapshot/payload_json do not read them).
    headers: dict = field(default_factory=dict)


@dataclass
class PlanoHttp:
    modo: str  # "unico" | "por_linha" | "lote"
    metodo: str
    url_destino: str  # URL of the template PATH (literal placeholders) - audit/preview
    requisicoes: list[Requisicao] = field(default_factory=list)
    concorrencia: int = 1
    taxa_por_segundo: float | None = None
    resposta_json_path: str | None = None
    colunas_pred: list[str] = field(default_factory=list)
    # Response extraction (por_linha/lote): (JSON path, column name). Empty = only `_resposta`.
    resposta_campos: tuple[tuple[str, str], ...] = ()
    incluir_resposta_completa: bool = True


# Columns the node itself writes to the snapshot; an extracted field cannot take their names.
COLUNAS_RESERVADAS = ("_payload", "_status", "_erro", "_resposta", "_chunk", "linhas_enviadas")

_INDICE = re.compile(r"^\d+$")


def extrair_valor(corpo: object, caminho: str) -> object:
    """Value at `caminho` (`a.b.0.c`) inside the ALREADY parsed response. Single source of the
    navigation: preview simulates through `resposta_exemplo` with this same function.

    A path that does not exist -> `None` (the column stays empty; not an error: an API answering
    4xx returns another format and the row still has to enter the snapshot with its `_status`).
    Object/array value -> JSON text, never `str(dict)`, so downstream can still parse it."""
    no = corpo
    for parte in (caminho or "").split("."):
        if no is None:
            return None
        if isinstance(no, list):
            if not _INDICE.match(parte):
                return None
            i = int(parte)
            no = no[i] if 0 <= i < len(no) else None
        elif isinstance(no, dict):
            no = no.get(parte)
        else:
            return None
    if isinstance(no, (dict, list)):
        return json.dumps(no, ensure_ascii=False, default=str)
    return no


def _campos_resposta(cfg: dict) -> tuple[tuple[str, str], ...]:
    lista = cfg.get("resposta_campos") or []
    return tuple(
        (str(c.get("de") or ""), str(c.get("para") or ""))
        for c in lista
        if isinstance(c, dict) and c.get("de") and c.get("para")
    )


def montar_requisicoes(
    cfg: dict, colunas_pred: list[str], linhas_pred: list[dict], params: dict | None, base_url: str
) -> PlanoHttp:
    """Build the request plan (pure). `base_url` = URL of the `api_rest` connection. No I/O."""
    params = params or {}
    modo = cfg.get("modo", "unico")
    metodo = cfg.get("metodo", "POST")
    caminho = cfg.get("caminho", "/")
    reqs: list[Requisicao] = []
    if modo == "unico":
        url = montar_url(base_url, _subst_caminho(caminho, None, params))
        corpo = _corpo(cfg, None, params, unico=True)
        headers = _headers_extra(cfg, None, params)
        reqs.append(Requisicao(metodo=metodo, url=url, corpo=corpo, indice=0, headers=headers))
    elif modo == "por_linha":
        for i, linha in enumerate(linhas_pred):
            url = montar_url(base_url, _subst_caminho(caminho, linha, params))
            corpo = _corpo(cfg, linha, params, unico=False)
            headers = _headers_extra(cfg, linha, params)
            reqs.append(
                Requisicao(
                    metodo=metodo,
                    url=url,
                    corpo=corpo,
                    indice=i,
                    linha=linha,
                    headers=headers,
                )
            )
    else:  # lote: row chunks; body = array (or merged object) of the chunk.
        tam = int(cfg.get("lote_tamanho", 500) or 500)
        url = montar_url(base_url, _subst_caminho(caminho, None, params))
        # lote_chave (col or list of cols): no chunk crosses a key value; rows are grouped by key
        # and chunked WITHIN each group. E.g. key=company guarantees the merge (which inherits
        # scalars from the 1st body) never mixes companies with different settings. Without a key:
        # a single group.
        chave = cfg.get("lote_chave")
        if chave:
            cols = [chave] if isinstance(chave, str) else list(chave)
            grupos: dict = {}
            ordem: list = []
            for linha in linhas_pred:
                k = tuple(linha.get(cc) for cc in cols)
                if k not in grupos:
                    grupos[k] = []
                    ordem.append(k)
                grupos[k].append(linha)
            blocos = [grupos[k] for k in ordem]
        else:
            blocos = [linhas_pred]
        c = 0
        for bloco in blocos:
            for j in range(0, len(bloco), tam):
                chunk = bloco[j : j + tam]
                # headers_extra resolved from the chunk's 1st row (not None): allows `{{col}}` per
                # batch, e.g. Authorization={{token}} (same token on every row, via cross join with
                # the auth). Without this the token would come out empty in the batch.
                headers = _headers_extra(cfg, chunk[0] if chunk else None, params)
                corpo = _corpo_lote(cfg, chunk, params)
                reqs.append(
                    Requisicao(
                        metodo=metodo,
                        url=url,
                        corpo=corpo,
                        indice=c,
                        linhas_enviadas=len(chunk),
                        headers=headers,
                    )
                )
                c += 1
    taxa = cfg.get("taxa_por_segundo")
    campos = _campos_resposta(cfg)
    # A name conflict fails HERE (building, which also runs in dry-run) and not when writing the
    # snapshot: an extracted field silently overwriting the input column would swap the data that
    # was sent for what the API answered, unnoticed.
    conflitos = [para for _, para in campos if para in colunas_pred or para in COLUNAS_RESERVADAS]
    if conflitos:
        raise ErroAplicacao(
            codigo="http_resposta_campo_conflito",
            mensagem=(
                "Response column conflicts with an existing column: "
                + ", ".join(sorted(set(conflitos)))
                + ". Choose another name."
            ),
            status=400,
        )
    return PlanoHttp(
        modo=modo,
        metodo=metodo,
        url_destino=montar_url(base_url, caminho),
        requisicoes=reqs,
        concorrencia=int(cfg.get("concorrencia", 1) or 1),
        taxa_por_segundo=float(taxa) if taxa else None,
        resposta_json_path=cfg.get("resposta_json_path"),
        colunas_pred=list(colunas_pred),
        resposta_campos=campos,
        incluir_resposta_completa=bool(cfg.get("incluir_resposta_completa", True)),
    )


# ---------------- Snapshot (per-mode schema: single source, same in preview and execution) -----


def payload_json(corpo: object | None) -> str | None:
    """Canonical body serialization for the `_payload` column. Single source: preview and execution
    call the SAME function, guaranteeing a byte-identical payload over the same sample."""
    return json.dumps(corpo, ensure_ascii=False, default=str) if corpo is not None else None


def colunas_snapshot(
    modo: str,
    colunas_pred: list[str],
    campos: tuple[tuple[str, str], ...] = (),
    incluir_resposta_completa: bool = True,
) -> list[str]:
    """Node snapshot columns per mode (`unico` is dynamic, a flattened response, and uses
    `achatar_unico`). `campos` = columns extracted from the response; `incluir_resposta_completa=False`
    drops the raw `_resposta` (useful when only one field matters and the response is large)."""
    extras = [para for _, para in campos]
    meta = ["_payload", "_status", "_erro"] + (["_resposta"] if incluir_resposta_completa else [])
    if modo == "por_linha":
        return list(colunas_pred) + extras + meta
    return ["_chunk", "linhas_enviadas"] + extras + meta  # lote


def linha_snapshot(
    modo: str,
    req: Requisicao,
    resultado: dict,
    colunas_pred: list[str],
    campos: tuple[tuple[str, str], ...] = (),
    incluir_resposta_completa: bool = True,
) -> dict:
    """One snapshot row (por_linha/lote). `resultado`: {status, resposta (raw), erro}.

    The `campos` come from the JSON response parsed ONCE per request. A non-JSON (or empty)
    response leaves the fields as `None` and does NOT fail: the row's `_status`/`_erro` remain the
    record of what happened, and failures are exactly when the response is usually not the
    expected JSON."""
    meta = {
        "_payload": payload_json(req.corpo),
        "_status": resultado.get("status"),
        "_erro": resultado.get("erro"),
    }
    if incluir_resposta_completa:
        meta["_resposta"] = resultado.get("resposta")
    extraidos: dict = {}
    if campos:
        try:
            corpo = json.loads(resultado.get("resposta") or "")
        except (ValueError, TypeError):
            corpo = None
        extraidos = {para: serial(extrair_valor(corpo, de)) for de, para in campos}
    if modo == "por_linha":
        base = {c: serial((req.linha or {}).get(c)) for c in colunas_pred}
    else:
        base = {"_chunk": req.indice, "linhas_enviadas": req.linhas_enviadas}
    return {**base, **extraidos, **meta}


def achatar_unico(
    raw: str, status: int | None, json_path: str | None
) -> tuple[list[str], list[dict]]:
    """`unico` mode: flatten the JSON response (via `resposta_json_path`) into rows + a `_status`
    column. A non-JSON response fails EXPLICITLY (cannot be flattened) instead of becoming silent
    data."""
    try:
        corpo = json.loads(raw) if raw else None
    except (ValueError, TypeError) as exc:
        raise ErroAplicacao(
            codigo="http_resposta_nao_json",
            mensagem="Response in 'unico' mode is not JSON and cannot be flattened into a table.",
            status=400,
        ) from exc
    linhas = _linhas(_extrair_array(corpo, json_path))
    for lin in linhas:
        lin["_status"] = status
    colunas = list(linhas[0].keys()) if linhas else ["_status"]
    return colunas, linhas


# ---------------- Efeito ----------------


async def executar_http(
    config: dict,
    senha: str | None,
    plano: PlanoHttp,
    *,
    timeout: float,
    concorrencia_max: int,
    tolerancia: str,
) -> tuple[list[dict | None], int]:
    """Fire the plan's requests. Returns `(resultados, disparados)`:
    - `resultados[i]` = {status, resposta (raw text), erro (None if ok)} or None (not fired, only
      under `abortar` after an earlier error);
    - `disparados` = how many requests actually went out (explicit partial effect under `abortar`).
    A single AsyncClient; concurrency = min(requested, cap); per-slot rate limit; per-request
    timeout. The auth header (`_headers`) goes only in the request, never in the result."""
    import httpx

    # STATIC connection headers (auth token included), resolved once. Dynamic ones (`req.headers`,
    # per request) are merged ON TOP at dispatch. None of them reaches the result.
    headers_base = _headers(config, senha)
    conc = max(1, min(int(plano.concorrencia or 1), int(concorrencia_max)))
    sem = asyncio.Semaphore(conc)
    abortar = tolerancia != "continuar"
    resultados: list[dict | None] = [None] * len(plano.requisicoes)
    parar = asyncio.Event()
    disparados = 0

    intervalo = 1.0 / plano.taxa_por_segundo if plano.taxa_por_segundo else 0.0
    proximo = [0.0]
    trava = asyncio.Lock()

    async def _aguardar_taxa() -> None:
        if intervalo <= 0:
            return
        async with trava:
            agora = time.monotonic()
            espera = proximo[0] - agora
            proximo[0] = (agora if espera <= 0 else proximo[0]) + intervalo
        if espera > 0:
            await asyncio.sleep(espera)

    async with httpx.AsyncClient(timeout=timeout) as cli:

        async def _uma(i: int, req: Requisicao) -> None:
            nonlocal disparados
            if parar.is_set():
                return
            async with sem:
                if parar.is_set():
                    return
                await _aguardar_taxa()
                if parar.is_set():
                    return
                disparados += 1
                try:
                    r = await cli.request(
                        req.metodo,
                        req.url,
                        headers={**headers_base, **req.headers},
                        json=req.corpo if req.corpo is not None else None,
                    )
                    r.raise_for_status()
                    resultados[i] = {"status": r.status_code, "resposta": r.text, "erro": None}
                except Exception as exc:  # noqa: BLE001 - sanitized cause, no secret/header leak
                    resp = getattr(exc, "response", None)
                    resultados[i] = {
                        "status": getattr(resp, "status_code", None),
                        "resposta": getattr(resp, "text", None),
                        "erro": _causa(exc),
                    }
                    if abortar:
                        parar.set()

        await asyncio.gather(*(_uma(i, req) for i, req in enumerate(plano.requisicoes)))
    return resultados, disparados
