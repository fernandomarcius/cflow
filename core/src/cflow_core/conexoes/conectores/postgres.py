"""PostgreSQL connector (asyncpg, natively asynchronous)."""

from __future__ import annotations

import asyncio

from ...config import get_settings
from ...errors import ErroAplicacao
from ..parametros import NUMERADO, vincular
from ..pool import postgres_conn
from .base import (
    ResultadoTeste,
    conferir_escopo,
    parametro_do_escopo,
    serial,
    valores_do_escopo,
)


def _q(ident: str) -> str:
    """Quotes a Postgres identifier (double quotes, with "" escaped). The identifier is already
    validated (`^[A-Za-z_][A-Za-z0-9_]*$`) by the definition validation; quoting is extra defence."""
    return '"' + ident.replace('"', '""') + '"'


def _afetados_pg(status: str) -> int:
    """Affected rows from the asyncpg status tag ('INSERT 0 42', 'UPDATE 42', ...)."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


def montar_sql_tabela_pg(
    schema: str,
    tabela: str,
    colunas_destino: list[str],
    estrategia: str,
    chave: list[str] | None,
    tipos: dict[str, str] | None = None,
) -> str:
    """Final SQL (pure) that moves the `_stage` staging table into the target, per strategy. The
    TRUNCATE of truncate_insert runs separately (same transaction). Reused by preview (dry-run) and
    execution.

    `tipos` (execution): map column -> Postgres type of the target. Staging is TEXT (in-flight DuckDB
    data is always VARCHAR, see engine.py) and asyncpg does NOT convert str -> int/date on bind; the
    text -> target-type cast (`'2'::smallint`) happens HERE, in the SQL. Without `tipos` (preview /
    dry-run, which has no connection to read the catalogue) the SQL has no cast, as it always did."""
    alvo = f"{_q(schema)}.{_q(tabela)}"
    cols = ", ".join(_q(c) for c in colunas_destino)
    sel = ", ".join(
        f"{_q(c)}::{tipos[c]}" if tipos and c in tipos else _q(c) for c in colunas_destino
    )
    base = f"INSERT INTO {alvo} ({cols}) SELECT {sel} FROM _stage"
    if estrategia in ("append", "truncate_insert"):
        return base
    if estrategia == "upsert":
        conflito = ", ".join(_q(c) for c in (chave or []))
        nonkey = [c for c in colunas_destino if c not in (chave or [])]
        if not nonkey:  # every column is a key: nothing to update
            return f"{base} ON CONFLICT ({conflito}) DO NOTHING"
        set_ = ", ".join(f"{_q(c)} = EXCLUDED.{_q(c)}" for c in nonkey)
        return f"{base} ON CONFLICT ({conflito}) DO UPDATE SET {set_}"
    raise ErroAplicacao(
        codigo="escrita_estrategia",
        mensagem=f"Strategy '{estrategia}' is not supported.",
        status=400,
    )


def _ref_escopo(ref: str, tipo: str | None) -> str:
    """Scope `:parametro`, with a cast to the target type when known. The value is bound as TEXT
    (asyncpg does not convert str -> int/date, same rule as staging) and the cast happens in the
    SQL: `'188'::text::integer`."""
    nome = parametro_do_escopo(ref)
    return f":{nome}::text::{tipo}" if tipo else f":{nome}"


def _literal(texto: str) -> str:
    return "'" + texto.replace("'", "''") + "'"


def montar_sql_limpeza_pg(
    schema: str, tabela: str, escopo: dict[str, str], tipos: dict[str, str] | None = None
) -> str:
    """Scope DELETE (`limpar_escopo`), in the SAME transaction as the write. Without `tipos` (preview,
    which does not read the target catalogue) it has no cast, same rule as `montar_sql_tabela_pg`."""
    tipos = tipos or {}
    pred = " AND ".join(f"{_q(c)} = {_ref_escopo(ref, tipos.get(c))}" for c, ref in escopo.items())
    return f"DELETE FROM {_q(schema)}.{_q(tabela)} WHERE {pred}"


def montar_sql_trava_escopo_pg(
    schema: str, tabela: str, escopo: dict[str, str], tipos: dict[str, str]
) -> str:
    """Transactional lock PER SCOPE, taken before the DELETE. Under READ COMMITTED a second run's
    DELETE does not see rows the first one just inserted and has not committed, and both end up
    summed in the scope; the lock makes the second wait for the first commit and only then delete.
    The key is the value ALREADY CONVERTED to the column type (`'0188'` and `'188'` are the same
    scope in the database and must contend for the same lock). A `hashtext` collision merely
    serialises more than needed."""
    partes = [_literal(f"{schema}.{tabela}")]
    for c, ref in escopo.items():
        partes += [_literal(c), f"({_ref_escopo(ref, tipos.get(c))})::text"]
    return f"SELECT pg_advisory_xact_lock(hashtext(concat_ws('|', {', '.join(partes)})))"


def montar_sql_fora_do_escopo_pg(escopo: dict[str, str], tipos: dict[str, str]) -> str:
    """Counts the staging (TEXT) rows that do NOT belong to the scope, comparing in the target type.
    A row outside the scope is never deleted by that scope's runs, so it would be a leftover by
    construction. NULL counts as outside (`COALESCE(..., false)`)."""
    igual = " AND ".join(
        f"{_q(c)}{'::' + tipos[c] if c in tipos else ''} = {_ref_escopo(ref, tipos.get(c))}"
        for c, ref in escopo.items()
    )
    return f"SELECT count(*) FROM _stage WHERE NOT COALESCE(({igual}), false)"


def _causa(exc: Exception) -> str:
    import asyncpg

    if isinstance(
        exc, (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError)
    ):
        return "authentication refused"
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return "database does not exist"
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return "host unreachable"
    return "failed to connect"


class ConectorPostgres:
    async def _abrir(self, config: dict, senha: str | None):
        import asyncpg

        return await asyncpg.connect(
            host=config.get("host"),
            port=int(config.get("porta", 5432)),
            user=config.get("usuario"),
            password=senha,
            database=config.get("base"),
            timeout=get_settings().conexao_teste_timeout_segundos,
        )

    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        try:
            conn = await self._abrir(config, senha)
            await conn.close()
            return ResultadoTeste(ok=True)
        except Exception as exc:  # noqa: BLE001 — sanitised cause, no secret leaked
            return ResultadoTeste(ok=False, causa=_causa(exc))

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        conn = await self._abrir(config, senha)
        try:
            rows = await conn.fetch(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY table_schema, table_name"
            )
        finally:
            await conn.close()
        return _agrupar(rows, "table_schema", "table_name")

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        conn = await self._abrir(config, senha)
        try:
            rows = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                schema,
                tabela,
            )
        finally:
            await conn.close()
        return [{"nome": r["column_name"], "tipo": r["data_type"]} for r in rows]

    async def amostra(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        limite: int | None,
        params: dict | None = None,
    ) -> tuple[list[str], list[dict]]:
        sql_b, vals = vincular(sql, params, NUMERADO)  # :nome -> $1.. + bind (asyncpg)
        usar_pool = get_settings().pool_fontes and limite is None  # pool only for execution reads
        async with postgres_conn(
            config, senha, lambda: self._abrir(config, senha), usar_pool
        ) as conn:
            lim = (
                f" LIMIT {int(limite)}" if limite is not None else ""
            )  # None = read everything (execution)
            # Prepared statement: columns come from the result schema (get_attributes) EVEN with
            # 0 rows, so the downstream DuckDB table has the real columns.
            stmt = await conn.prepare(f"SELECT * FROM ({sql_b}) AS _sub{lim}")
            colunas = [a.name for a in stmt.get_attributes()]
            rows = await stmt.fetch(*vals)
        # Per-value `serial` OFF the event loop: in execution this is the whole source, and converting
        # it on the loop stalled every node running alongside (measured on the SQL Server connector,
        # which had the same design; see `conexoes/colunar.py`).
        return colunas, await asyncio.to_thread(
            lambda: [{k: serial(v) for k, v in dict(r).items()} for r in rows]
        )

    async def _abrir_escrita(self, config: dict, senha: str | None):
        import asyncpg

        cfg = get_settings()
        return await asyncpg.connect(
            host=config.get("host"),
            port=int(config.get("porta", 5432)),
            user=config.get("usuario"),
            password=senha,
            database=config.get("base"),
            timeout=cfg.conexao_teste_timeout_segundos,
            command_timeout=cfg.conexao_query_timeout_segundos,
        )

    async def _tipos_colunas(
        self, conn, schema: str, tabela: str, colunas: list[str]
    ) -> dict[str, str]:
        """Map column -> Postgres type of the target (with modifiers: `numeric(10,2)`, `varchar(20)`),
        via `format_type`, a string ready to use in a `::type` cast. Only the requested columns."""
        rows = await conn.fetch(
            "SELECT a.attname AS nome, format_type(a.atttypid, a.atttypmod) AS tipo "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped",
            schema,
            tabela,
        )
        m = {r["nome"]: r["tipo"] for r in rows}
        return {c: m[c] for c in colunas if c in m}

    async def escrever_tabela(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
    ) -> int:
        afetados, _ = await self._escrever(
            config,
            senha,
            schema=schema,
            tabela=tabela,
            colunas_destino=colunas_destino,
            linhas=linhas,
            estrategia=estrategia,
            chave=chave,
            lote_tamanho=lote_tamanho,
            escopo=None,
            valores_escopo=None,
        )
        return afetados

    async def escrever_tabela_no_escopo(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
        escopo: dict[str, str],
        params: dict,
    ) -> dict:
        # Refuse BEFORE opening a connection: a malformed scope or a parameter without a value must
        # never reach a DELETE, neither `WHERE col = NULL` (deletes nothing and writes on top) nor no WHERE.
        conferir_escopo(escopo, estrategia, colunas_destino)
        valores = valores_do_escopo(escopo, params)
        afetados, apagados = await self._escrever(
            config,
            senha,
            schema=schema,
            tabela=tabela,
            colunas_destino=colunas_destino,
            linhas=linhas,
            estrategia=estrategia,
            chave=chave,
            lote_tamanho=lote_tamanho,
            escopo=escopo,
            valores_escopo=valores,
        )
        return {"linhas_afetadas": afetados, "linhas_apagadas": apagados}

    async def _limpar_escopo(
        self, conn, schema: str, tabela: str, escopo: dict, valores: dict, tipos: dict
    ) -> int:
        """Checks staging against the scope, locks the scope and deletes, all inside the write
        transaction, after `_stage` is loaded (the lock lasts only DELETE + write)."""
        sem_tipo = [c for c in escopo if c not in tipos]
        if sem_tipo:
            raise ErroAplicacao(
                codigo="escrita_escopo_coluna_inexistente",
                mensagem=(
                    f"limpar_escopo columns that do not exist in {schema}.{tabela}: "
                    + ", ".join(sem_tipo)
                    + "."
                ),
                status=400,
            )
        texto = {k: str(v) for k, v in valores.items()}  # TEXT bind; the cast is in the SQL
        sql_f, vals_f = vincular(montar_sql_fora_do_escopo_pg(escopo, tipos), texto, NUMERADO)
        fora = await conn.fetchval(sql_f, *vals_f)
        if fora:
            raise ErroAplicacao(
                codigo="escrita_fora_do_escopo",
                mensagem=(
                    f"{fora} input row(s) outside the scope that the write clears "
                    f"({', '.join(escopo)}). Writing them would leave rows that no run of this "
                    "scope deletes."
                ),
                status=400,
            )
        sql_t, vals_t = vincular(
            montar_sql_trava_escopo_pg(schema, tabela, escopo, tipos), texto, NUMERADO
        )
        await conn.execute(sql_t, *vals_t)
        sql_d, vals_d = vincular(
            montar_sql_limpeza_pg(schema, tabela, escopo, tipos), texto, NUMERADO
        )
        return _afetados_pg(await conn.execute(sql_d, *vals_d))

    async def _escrever(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
        escopo: dict[str, str] | None,
        valores_escopo: dict | None,
    ) -> tuple[int, int | None]:
        """(rows written, rows deleted from the scope; None without `limpar_escopo`)."""
        alvo = f"{_q(schema)}.{_q(tabela)}"
        cols = ", ".join(_q(c) for c in colunas_destino)
        phs = ", ".join(f"${i + 1}" for i in range(len(colunas_destino)))
        # In-flight data is VARCHAR (DuckDB, engine.py), so staging is TEXT and values are str:
        # asyncpg requires the Python type to match the bind type (str -> text ok; str -> smallint
        # fails). NULL is preserved.
        registros = [
            tuple(None if lin.get(c) is None else str(lin.get(c)) for c in colunas_destino)
            for lin in linhas
        ]
        lote = max(1, int(lote_tamanho))
        afetados = 0
        apagados = None
        conn = await self._abrir_escrita(config, senha)
        try:
            async with conn.transaction():
                # The scope columns are included in the catalogue read: the DELETE and the staging
                # check compare in the target type.
                pedidas = list(dict.fromkeys([*colunas_destino, *(escopo or {})]))
                tipos = await self._tipos_colunas(conn, schema, tabela, pedidas)
                # TEXT staging (does not inherit target types): the text -> real type cast is in the final SQL.
                defs = ", ".join(f"{_q(c)} text" for c in colunas_destino)
                await conn.execute(f"CREATE TEMP TABLE _stage ({defs}) ON COMMIT DROP")
                ins = f"INSERT INTO _stage ({cols}) VALUES ({phs})"
                for i in range(0, len(registros), lote):
                    await conn.executemany(ins, registros[i : i + lote])
                if escopo is not None:
                    apagados = await self._limpar_escopo(
                        conn, schema, tabela, escopo, valores_escopo or {}, tipos
                    )
                if estrategia == "truncate_insert":
                    await conn.execute(f"TRUNCATE TABLE {alvo}")
                final = montar_sql_tabela_pg(
                    schema, tabela, colunas_destino, estrategia, chave, tipos
                )
                afetados = _afetados_pg(await conn.execute(final))
            # End of the block = ONE commit: scope clearing and the write persist together or not at all.
            return afetados, apagados
        except ErroAplicacao:
            raise
        except Exception as exc:  # noqa: BLE001 — automatic rollback; sanitised cause (no secret)
            raise ErroAplicacao(
                codigo="escrita_falhou",
                mensagem="Failed to write to the external destination.",
                status=400,
            ) from exc
        finally:
            await conn.close()

    async def executar_dml(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        params: dict | None,
        entrada: tuple[list[str], list[dict]] | None,
    ) -> int:
        conn = await self._abrir_escrita(config, senha)
        try:
            async with conn.transaction():
                if entrada is not None:
                    cols, linhas = entrada
                    coldef = ", ".join(f"{_q(c)} text" for c in cols)
                    await conn.execute(f"CREATE TEMP TABLE _entrada ({coldef}) ON COMMIT DROP")
                    if linhas:
                        colq = ", ".join(_q(c) for c in cols)
                        phs = ", ".join(f"${i + 1}" for i in range(len(cols)))
                        ins = f"INSERT INTO _entrada ({colq}) VALUES ({phs})"
                        regs = [
                            tuple(None if lin.get(c) is None else str(lin.get(c)) for c in cols)
                            for lin in linhas
                        ]
                        for i in range(0, len(regs), 1000):
                            await conn.executemany(ins, regs[i : i + 1000])
                sql_b, vals = vincular(sql, params, NUMERADO)
                afetados = _afetados_pg(await conn.execute(sql_b, *vals))
            return afetados
        except ErroAplicacao:
            raise
        except Exception as exc:  # noqa: BLE001 — automatic rollback; sanitised cause (no secret)
            raise ErroAplicacao(
                codigo="escrita_falhou",
                mensagem="Failed to run the DML on the destination.",
                status=400,
            ) from exc
        finally:
            await conn.close()


def _agrupar(rows, kschema: str, ktabela: str) -> list[dict]:
    schemas: dict[str, list[str]] = {}
    for r in rows:
        schemas.setdefault(r[kschema], []).append(r[ktabela])
    return [{"schema": s, "tabelas": t} for s, t in schemas.items()]
