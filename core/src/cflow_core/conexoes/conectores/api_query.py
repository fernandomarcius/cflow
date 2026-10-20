"""SQL-over-REST gateway connector (e.g. a query app at https://gateway.example.com).

Flow: login (user + password) -> Bearer token -> POST {sql} -> results[]. Used for sources exposed
as views behind the gateway. The gateway receives the SQL as a STRING (no native binding), so
parameters are interpolated as escaped literals (`parametros.interpolar_literais`). The login
password is resolved only at runtime, from the vault, and never echoed; errors are sanitised
(no leaked secret or full URL).

config: {
  url,                       # base, e.g. "https://gateway.example.com"
  login_path="/api/v1/auth/login",
  query_path="/api/v1/query",
  usuario,                   # login username
  token_campo="token",       # login JSON field holding the token
  sql_campo="sql",           # query body field
  results_campo="results",   # response JSON field holding the rows
}
secret (vault): login password.
"""

from __future__ import annotations

import asyncio
import time as _clock

from ...config import get_settings
from ...errors import ErroAplicacao
from ...retry import espera as _backoff
from ..parametros import interpolar_literais
from .base import ResultadoTeste, serial

# Gateway throttle (protects against fan-out bursts): a semaphore PER loop (concurrency) plus a
# Bearer token cache (avoids re-login on every call, which would hit 429 on login). Process-local state.
_sems: dict[int, asyncio.Semaphore] = {}
_tokens: dict[tuple, tuple[str, float]] = {}  # (base, usuario) -> (token, expires_at monotonic)


def _semaforo() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    s = _sems.get(id(loop))
    if s is None:
        s = asyncio.Semaphore(max(1, get_settings().gateway_max_concorrencia))
        _sems[id(loop)] = s
    return s


def _causa(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "authentication refused"
        return f"the gateway responded {code}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "host unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, KeyError):
        return "gateway response is missing the expected field (token/results)"
    return "failed to query the gateway"


def _base(config: dict) -> str:
    return str(config.get("url", "")).rstrip("/")


def _colunas_do_select(sql: str) -> list[str]:
    """Extracts column names from the outer SELECT. Used when the gateway returns 0 rows (it declares
    no schema): recovers the columns from the SQL itself so downstream does not break.
    Simple heuristic (split on commas at depth 0; `AS alias` or last identifier)."""
    import re

    s = sql.strip()
    if not s.upper().startswith("SELECT"):
        return []
    # Find the first FROM at depth 0 (a word, with any whitespace around it; the SQL may be
    # multiline, so a literal " FROM " cannot be matched).
    ws = " \t\n\r"
    proj, depth, in_str, i = s[6:], 0, False, 6
    from_at = -1
    while i < len(s):
        ch = s[i]
        if ch == "'":
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif (
                depth == 0
                and s[i : i + 4].upper() == "FROM"
                and (i == 0 or s[i - 1] in ws)
                and (i + 4 >= len(s) or s[i + 4] in ws + "(")
            ):
                from_at = i
                break
        i += 1
    if from_at > 0:
        proj = s[6:from_at]
    # Split on commas at depth 0.
    cols, depth, in_str, cur = [], 0, False, ""
    for ch in proj:
        if ch == "'":
            in_str = not in_str
            cur += ch
        elif not in_str and ch == "(":
            depth += 1
            cur += ch
        elif not in_str and ch == ")":
            depth -= 1
            cur += ch
        elif not in_str and ch == "," and depth == 0:
            cols.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        cols.append(cur)
    nomes = []
    for c in cols:
        c = c.strip()
        m = re.search(r'\bAS\s+"?([A-Za-z_][A-Za-z0-9_ ]*?)"?\s*$', c, re.I)
        if m:
            nomes.append(m.group(1).strip())
            continue
        c2 = re.sub(r"::[A-Za-z0-9_ ]+$", "", c).strip().strip('"')
        nomes.append(re.split(r"[\s.]", c2)[-1].strip('"'))
    return [n for n in nomes if n]


class ConectorApiQuery:
    async def _login(self, cli, config: dict, senha: str | None) -> str:
        url = _base(config) + config.get("login_path", "/api/v1/auth/login")
        corpo = {"username": config.get("usuario"), "password": senha}
        tentativas = max(1, get_settings().gateway_max_tentativas)
        for tent in range(tentativas):
            r = await cli.post(url, json=corpo, headers={"Content-Type": "application/json"})
            if r.status_code == 429 and tent < tentativas - 1:
                await asyncio.sleep(_backoff(tent))  # login rate-limited: wait and retry
                continue
            r.raise_for_status()
            return r.json()[config.get("token_campo", "token")]
        r.raise_for_status()  # attempts exhausted (429): propagate
        return r.json()[config.get("token_campo", "token")]

    async def _token(self, cli, config: dict, senha: str | None, *, forcar: bool = False) -> str:
        """Bearer token with a cache (reused across calls in the same process), avoiding a re-login
        per call (login is rate-limited). `forcar` logs in again (e.g. after a 401)."""
        chave = (_base(config), str(config.get("usuario")))
        agora = _clock.monotonic()
        if not forcar:
            cache = _tokens.get(chave)
            if cache and cache[1] > agora:
                return cache[0]
        tok = await self._login(cli, config, senha)
        _tokens[chave] = (tok, agora + get_settings().gateway_token_ttl_segundos)
        return tok

    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        import httpx

        timeout = get_settings().conexao_teste_timeout_segundos
        try:
            async with httpx.AsyncClient(timeout=timeout) as cli:
                tok = await self._login(cli, config, senha)
            return ResultadoTeste(ok=bool(tok))
        except Exception as exc:  # noqa: BLE001 — sanitised cause
            return ResultadoTeste(ok=False, causa=_causa(exc))

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        raise ErroAplicacao(
            codigo="nao_aplicavel",
            mensagem="API gateway has no schema browsing (use a view in the SQL).",
            status=400,
        )

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        raise ErroAplicacao(
            codigo="nao_aplicavel",
            mensagem="API gateway does not browse columns; run a SELECT on the view.",
            status=400,
        )

    async def amostra(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        limite: int | None,
        params: dict | None = None,
    ) -> tuple[list[str], list[dict]]:
        import httpx

        # the gateway receives SQL as a string, so parameters become escaped literals
        sql_final = interpolar_literais(sql, params)
        # Execution (limite None) = long query, large timeout; preview/sample = medium (a real
        # query, not just "connection alive"; the 5s connection-test timeout blows up on heavy sources).
        cfg = get_settings()
        timeout = (
            cfg.conexao_query_timeout_segundos
            if limite is None
            else cfg.conexao_amostra_timeout_segundos
        )
        tentativas = max(1, cfg.gateway_max_tentativas)
        query_url = _base(config) + config.get("query_path", "/api/v1/query")
        # The semaphore caps simultaneous gateway calls (fan-out burst -> at most N concurrent).
        async with _semaforo():
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = None
                relogar = False
                for tent in range(tentativas):
                    tok = await self._token(cli, config, senha, forcar=relogar)
                    relogar = False
                    r = await cli.post(
                        query_url,
                        json={config.get("sql_campo", "sql"): sql_final},
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {tok}",
                        },
                    )
                    if r.status_code == 401 and tent < tentativas - 1:
                        relogar = True  # expired/invalid token: log in again and retry
                        continue
                    if r.status_code == 429 and tent < tentativas - 1:
                        # Gateway saturated/over quota: REGENERATE the token (invalidates the cache) and
                        # retry after backoff; the new token may reopen the quota window.
                        relogar = True
                        await asyncio.sleep(_backoff(tent))
                        continue
                    break
                if r.status_code >= 400:
                    # The gateway body carries the REAL cause of the query (e.g. missing view/column,
                    # type error). Surface it (truncated): it is not a secret (SQL/database error),
                    # but the URL/credential stay out of the message.
                    detalhe = " ".join((r.text or "").split())
                    if len(detalhe) > 600:
                        detalhe = detalhe[:600] + "…"
                    raise ErroAplicacao(
                        codigo="gateway_query_erro",
                        mensagem=f"The gateway responded {r.status_code} while running the source "
                        f"query. Cause: {detalhe or 'no response body'}",
                        status=502,
                    )
                linhas = r.json().get(config.get("results_campo", "results"), []) or []
        if limite:
            linhas = linhas[:limite]
        saneadas = [{k: serial(v) for k, v in row.items()} for row in linhas]
        # With rows: columns of the first row; WITHOUT rows the gateway declares no schema, so extract
        # them from the SQL (otherwise downstream breaks referencing columns in a period with no data).
        colunas = list(saneadas[0].keys()) if saneadas else _colunas_do_select(sql_final)
        return colunas, saneadas
