"""REST API source connector (httpx, async). Materialises the JSON response as a table.

config: {url, metodo?, headers?, auth_tipo? ('bearer'|'header'|'none'), auth_header?, json_path?,
         paginacao? {tipo:'pagina', param, tamanho_param, paginas}}.
The secret (token/key) comes from the vault and is injected into the auth header, never logged in plaintext.
"""

from __future__ import annotations

from ...config import get_settings
from .base import ResultadoTeste, serial


def _headers(config: dict, senha: str | None) -> dict:
    headers = dict(config.get("headers") or {})
    tipo = config.get("auth_tipo", "none")
    if senha and tipo == "bearer":
        headers["Authorization"] = f"Bearer {senha}"
    elif senha and tipo == "header":
        headers[config.get("auth_header", "X-API-Key")] = senha
    return headers


def _extrair_array(corpo, json_path: str | None) -> list:
    """Walks to the list inside the JSON (json_path = 'a.b.c'); if absent, uses the body when it is a list."""
    no = corpo
    for parte in json_path.split(".") if json_path else []:
        no = no.get(parte) if isinstance(no, dict) else None
    if no is None:
        no = corpo
    return no if isinstance(no, list) else [no]


def _linhas(itens: list) -> list[dict]:
    out = []
    for it in itens:
        if isinstance(it, dict):
            out.append({k: serial(v) for k, v in it.items()})
        else:
            out.append({"valor": serial(it)})
    return out


def _causa(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "authentication refused"
        return f"HTTP response {code}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "host unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return "failed to access the API"


class ConectorApiRest:
    async def _buscar(
        self, config: dict, senha: str | None, limite: int | None, extra_params: dict | None = None
    ) -> list[dict]:
        import httpx

        timeout = get_settings().conexao_teste_timeout_segundos
        metodo = config.get("metodo", "GET")
        json_path = config.get("json_path")
        pag = config.get("paginacao")
        base_params = dict(extra_params or {})  # runtime parameters -> query string
        linhas: list[dict] = []
        async with httpx.AsyncClient(timeout=timeout) as cli:
            if pag and pag.get("tipo") == "pagina":
                for pagina in range(1, int(pag.get("paginas", 1)) + 1):
                    params = {
                        **base_params,
                        pag["param"]: pagina,
                        pag.get("tamanho_param", "tamanho"): pag.get("tamanho", 100),
                    }
                    r = await cli.request(
                        metodo, config["url"], headers=_headers(config, senha), params=params
                    )
                    r.raise_for_status()
                    novas = _linhas(_extrair_array(r.json(), json_path))
                    if not novas:
                        break
                    linhas.extend(novas)
                    if limite and len(linhas) >= limite:
                        break
            else:
                r = await cli.request(
                    metodo,
                    config["url"],
                    headers=_headers(config, senha),
                    params=base_params or None,
                )
                r.raise_for_status()
                linhas = _linhas(_extrair_array(r.json(), json_path))
        return linhas[:limite] if limite else linhas

    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        try:
            await self._buscar(config, senha, limite=1)
            return ResultadoTeste(ok=True)
        except Exception as exc:  # noqa: BLE001 — sanitised cause
            return ResultadoTeste(ok=False, causa=_causa(exc))

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        from ...errors import ErroAplicacao

        raise ErroAplicacao(
            codigo="nao_aplicavel", mensagem="API source has no schema browsing.", status=400
        )

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        linhas = await self._buscar(config, senha, limite=1)
        return [
            {"nome": k, "tipo": type(v).__name__} for k, v in (linhas[0].items() if linhas else [])
        ]

    async def amostra(
        self, config: dict, senha: str | None, sql: str, limite: int, params: dict | None = None
    ) -> tuple[list[str], list[dict]]:
        # 'sql' does not apply to a REST API: the source is the configured URL; params become the query string.
        linhas = await self._buscar(config, senha, limite=limite, extra_params=params)
        colunas = (
            list(linhas[0].keys()) if linhas else []
        )  # the API declares no schema, so use the first row
        return colunas, linhas
