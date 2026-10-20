"""Connections: registry, listing, testing and schema browsing. The secret lives only in the vault,
resolved at runtime when testing/browsing; it never returns to user-facing surfaces or to the audit diff.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..db import transacao
from ..errors import DetalheErro, ErroAplicacao
from ..vault import service as vault
from .colunar import Extracao
from .conectores import TIPOS_EMAIL, TIPOS_ESCRITA_SQL, get_conector
from .conectores import smtp as conector_smtp

TIPOS = ("sqlserver", "postgres", "api_rest", "api_query", "arquivo", "smtp", "objeto")


def _validar_config(tipo: str, config: dict) -> None:
    """Validates the config per type BEFORE any network call (registration mistakes are more common
    than server ones). `smtp` and `objeto` (lake destination) declare their own rules; the other
    types have no structural check."""
    if tipo == "smtp":
        problemas = conector_smtp.validar_config(config)
    elif tipo == "objeto":
        from ..ingestao import armazenamento

        problemas = armazenamento.validar_config(config)
    else:
        problemas = []
    if problemas:
        raise ErroAplicacao(
            codigo="config_invalida",
            mensagem="Invalid connection configuration.",
            status=422,
            detalhes=[DetalheErro(campo="config", mensagem=m) for m in problemas],
        )


async def criar(
    conn: AsyncConnection,
    *,
    nome: str,
    tipo: str,
    config: dict,
    segredo: str | None,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> uuid.UUID:
    if tipo not in TIPOS:
        raise ErroAplicacao(codigo="tipo_invalido", mensagem="Invalid connection type.", status=400)
    if "senha" in config or "password" in config or "segredo" in config:
        raise ErroAplicacao(
            codigo="segredo_em_config", mensagem="The config must not contain a secret.", status=400
        )
    _validar_config(tipo, config)
    existe = (
        await conn.execute(text("SELECT 1 FROM conexoes WHERE nome = :n"), {"n": nome})
    ).first()
    if existe is not None:
        raise ErroAplicacao(
            codigo="nome_em_uso", mensagem="Connection name already in use.", status=409
        )

    ref = await vault.guardar(conn, segredo) if segredo else None
    cid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO conexoes (id, nome, tipo, config, credencial_ref, criado_por, ativo) "
            "VALUES (:id, :n, :t, :c, :r, :por, true)"
        ),
        {
            "id": cid,
            "n": nome,
            "t": tipo,
            "c": _json(config),
            "r": str(ref) if ref else None,
            "por": ator_id,
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="conexao",
        recurso_id=cid,
        ator_id=ator_id,
        antes=None,
        depois={"nome": nome, "tipo": tipo, "config": config},  # SEM segredo
        request_id=request_id,
        ip=ip,
    )
    return cid


def _json(d: dict) -> str:
    import json

    return json.dumps(d, ensure_ascii=False)


async def listar(conn: AsyncConnection) -> list[dict]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, nome, tipo, config, ativo, permitir_escrita FROM conexoes ORDER BY nome"
                )
            )
        )
        .mappings()
        .all()
    )
    # never returns credencial_ref or the secret
    return [
        {
            "id": str(r["id"]),
            "nome": r["nome"],
            "tipo": r["tipo"],
            "config": r["config"],
            "ativo": r["ativo"],
            "permitir_escrita": r["permitir_escrita"],
        }
        for r in rows
    ]


async def _carregar(conn: AsyncConnection, conexao_id: uuid.UUID):
    return (
        await conn.execute(
            text("SELECT tipo, config, credencial_ref FROM conexoes WHERE id = :id"),
            {"id": conexao_id},
        )
    ).first()


async def _resolver_senha(conn: AsyncConnection, c) -> str | None:
    return await vault.resolver(conn, uuid.UUID(c.credencial_ref)) if c.credencial_ref else None


async def _carregar_ou_404(conn: AsyncConnection, conexao_id: uuid.UUID):
    c = await _carregar(conn, conexao_id)
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    return c


@dataclass(frozen=True)
class Acesso:
    """Everything the connector needs to talk to the source: type, config and the already-resolved secret.

    It exists so that reading the source happens OUTSIDE any metadata-database transaction
    (see `obter_acesso`). The secret lives in the process for the duration of the call and is never
    serialised: it does not enter logs, API responses or audit diffs.
    """

    tipo: str
    config: dict
    senha: str | None


#: Access resolved per connection, per process, with a short TTL: the SAME yardstick as the lake
#: backend (`ingestao.armazenamento._BACKENDS`), including invalidation.
#:
#: Each call is a transaction plus a vault decryption, and a single run can have on the order of a
#: hundred source nodes asking for access to the same few connections (measured at roughly a quarter
#: of the run's database round trips). `Acesso` is a frozen dataclass with no per-call state, so the
#: instance is shareable.
#:
#: The credential stays in the memory of the process that resolved it, for the TTL: the same
#: exposure the lake backend and the cloud-storage token cache already have.
#: `editar`/`rotacionar_credencial`/`excluir` forget it immediately IN THIS process; in the others
#: (api vs worker), the TTL is the ceiling on how long a rotated credential stays in use.
_ACESSOS: dict[str, tuple[float, Acesso]] = {}
_TTL_ACESSO_S = 60.0
_TETO_ACESSOS = 256


async def obter_acesso(conexao_id: uuid.UUID) -> Acesso:
    """Loads the connection and resolves the credential in a SHORT transaction that closes before
    any external I/O.

    This is the heart of the separation: an earlier version received the caller's `conn` and called
    the connector with it still open, so the metadata transaction stayed alive throughout the source
    read (minutes, on a large SQL Server). An open transaction holds the whole database's `xmin`:
    while it exists, autovacuum cannot reclaim dead tuples of ANY table. With one execution a day
    that was fine; with several overlapping executions a day, `xmin` stays pinned almost all the time.

    The result is cached with a short TTL (`_ACESSOS`); see the note there.
    """
    chave = str(conexao_id)
    agora = time.monotonic()
    em_cache = _ACESSOS.get(chave)
    if em_cache is not None and agora - em_cache[0] < _TTL_ACESSO_S:
        return em_cache[1]
    async with transacao() as conn:
        c = await _carregar_ou_404(conn, conexao_id)
        acesso = Acesso(tipo=c.tipo, config=c.config, senha=await _resolver_senha(conn, c))
    if len(_ACESSOS) >= _TETO_ACESSOS:
        _ACESSOS.clear()
    _ACESSOS[chave] = (agora, acesso)
    return acesso


def esquecer_acesso(conexao_id: uuid.UUID | str) -> None:
    """Drops the cached access of the connection (config/credential changed, or it was deleted)."""
    _ACESSOS.pop(str(conexao_id), None)


async def testar(conexao_id: uuid.UUID) -> dict:
    """Tests the connection (resolves the credential at runtime). Dispatch by type; sanitised cause."""
    a = await obter_acesso(conexao_id)
    return (await get_conector(a.tipo).testar(a.config, a.senha)).dict()


async def navegar_schema(conexao_id: uuid.UUID) -> dict:
    """Lists schemas and tables (read-only), without materialising business data."""
    a = await obter_acesso(conexao_id)
    return {"schemas": await get_conector(a.tipo).schemas_tabelas(a.config, a.senha)}


async def colunas(conexao_id: uuid.UUID, schema: str, tabela: str) -> list[dict]:
    """Lists the columns/types of a table of a connection (read-only)."""
    a = await obter_acesso(conexao_id)
    return await get_conector(a.tipo).colunas(a.config, a.senha, schema, tabela)


async def amostra(
    conexao_id: uuid.UUID, sql: str, limite: int | None, params: dict | None = None
) -> tuple[list[str], list[dict]]:
    """Sample of a source (Studio preview / execution). Dispatch by type. Returns
    `(colunas, linhas)`; columns come from the result schema and exist even with 0 rows.
    `params` = declared runtime parameters; native binding in the connector (never concatenated).

    Deliberately takes NO `conn`: reading the source must happen with the metadata transaction
    already closed (see `obter_acesso`). Calling it from inside an `async with transacao()` still
    works, but takes a second pool connection and pins the first again; if you need to store
    something with the result, close the transaction first and open another afterwards.
    """
    a = await obter_acesso(conexao_id)
    return await get_conector(a.tipo).amostra(a.config, a.senha, sql, limite, params)


async def extrair(
    conexao_id: uuid.UUID,
    sql: str,
    params: dict | None = None,
    *,
    acesso: Acesso | None = None,
    paralela: dict | None = None,
) -> Extracao:
    """COMPLETE read of a source for EXECUTION (not a preview: no limit).

    A connector that can extract columnar (`extrair_tabela`, currently SQL Server) delivers an Arrow
    text table, batch by batch from the cursor; the others go through THIS module's `amostra`, as
    always (that is where anything spying on the extraction sees it). The text is the same on both
    paths; see `conexoes/colunar.py`. Same discipline as `amostra`: the metadata transaction closes
    before the source is read. An `acesso` already resolved by the caller saves a second vault trip."""
    a = acesso or await obter_acesso(conexao_id)
    extrair_tabela = getattr(get_conector(a.tipo), "extrair_tabela", None)
    if extrair_tabela is None:
        colunas, linhas = await amostra(conexao_id, sql, None, params)
        return Extracao(colunas, linhas=linhas)
    return await extrair_tabela(a.config, a.senha, sql, params, paralela=paralela)


#: Connectors that bind runtime parameters BY REFERENCE (`:nome` in the SQL becomes a bind; see
#: `parametros.vincular`): an extraction result depends only on the parameters the SQL cites.
#: `api_rest` is NOT included: it sends every runtime parameter as a query string.
TIPOS_VINCULO_POR_REFERENCIA = frozenset({"postgres", "sqlserver"})


#: `tipo` per connection, per process. The type is IMMUTABLE (`editar` changes name, config,
#: credential and `permitir_escrita`, never the type), so there is nothing to invalidate; the cap
#: only keeps the dictionary from growing without bound in a long-lived worker.
_TIPO_DA_CONEXAO: dict[str, str] = {}
_TETO_TIPOS = 4096


async def vincula_por_referencia(conexao_id: uuid.UUID) -> bool:
    """Does this connection's extraction depend only on the parameters cited in the SQL? Without
    resolving a credential (it is a metadata lookup, not source access). It used to be one
    transaction per connection source, hundreds per run, to re-read the same immutable value."""
    chave = str(conexao_id)
    tipo = _TIPO_DA_CONEXAO.get(chave)
    if tipo is None:
        async with transacao() as conn:
            c = await _carregar(conn, conexao_id)
        if c is None:
            return False
        if len(_TIPO_DA_CONEXAO) >= _TETO_TIPOS:
            _TIPO_DA_CONEXAO.clear()
        tipo = _TIPO_DA_CONEXAO[chave] = c.tipo
    return tipo in TIPOS_VINCULO_POR_REFERENCIA


def _esquecer_backend(conexao_id: uuid.UUID) -> None:
    """Config/credential changed (or the connection is gone): the lake backend cached IN THIS
    process stops being valid at once; in the others, it expires by TTL (`armazenamento.abrir`)."""
    from ..ingestao import armazenamento  # late import: storage imports this module

    armazenamento.esquecer(conexao_id)
    esquecer_acesso(conexao_id)


async def caminho_parquet_direto(conexao_id: uuid.UUID) -> str | None:
    """File path when the connection is of type `arquivo` in **parquet** format; otherwise `None`.

    It exists so the engine reads the parquet with `read_parquet` instead of going through the
    generic path (`amostra` -> `serial` -> JSON -> `read_json(columns=VARCHAR)`), which **destroys the
    declared type**: a `DECIMAL(6,2)` reaches the node SQL as the text `'1000.0'`, because `serial`
    already applied `float(Decimal)` before the JSON.

    Why it matters and why only for parquet: another pipeline's output and the lake dataset are
    already materialised by `read_parquet` (typed). Comparing both sides, which is what a ZERO
    tolerance gate does, with one side typed and the other all VARCHAR makes the `CAST` written in
    the comparison SQL decide the cent. Measured: three casts of the same column, three numbers.

    CSV and Excel **stay** on the VARCHAR path, on purpose: there is no declared type to preserve,
    and VARCHAR is what keeps auto-inference from eating the leading zero of an identifier (see
    `_carregar`). Parquet is the opposite case: the type was written into the file."""
    return caminho_parquet_do_acesso(await obter_acesso(conexao_id))


def caminho_parquet_do_acesso(a: Acesso) -> str | None:
    """`caminho_parquet_direto` for callers that already hold the `Acesso` (no new transaction)."""
    if a.tipo != "arquivo":
        return None
    from .conectores.arquivo import _formato

    if _formato(a.config) != "parquet":
        return None
    return a.config.get("caminho")


async def excluir(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    ator_id: uuid.UUID,
    ator_perfil: str | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Deletes the connection and its vault credential (same transaction; audited). 404 if missing.

    Authorisation at the edge: only the **creator** (criado_por) or an **admin** may delete; denial
    is a generic 403. A connection has NO versions. No secret in the diff."""
    c = (
        await conn.execute(
            text("SELECT nome, tipo, credencial_ref, criado_por FROM conexoes WHERE id = :id"),
            {"id": conexao_id},
        )
    ).first()
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    if ator_perfil != "admin" and c.criado_por != ator_id:
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)

    await conn.execute(text("DELETE FROM conexoes WHERE id = :id"), {"id": conexao_id})
    _esquecer_backend(conexao_id)
    if c.credencial_ref:
        await conn.execute(
            text("DELETE FROM cofre_credenciais WHERE id = CAST(:r AS uuid)"),
            {"r": str(c.credencial_ref)},
        )
    await registrar(
        conn,
        acao=Acao.EXCLUIR,
        recurso_tipo="conexao",
        recurso_id=conexao_id,
        ator_id=ator_id,
        antes={"nome": c.nome, "tipo": c.tipo},
        depois=None,  # NO secret
        request_id=request_id,
        ip=ip,
    )


async def _carregar_meta_ou_403(
    conn: AsyncConnection, conexao_id: uuid.UUID, ator_id: uuid.UUID, ator_perfil: str | None
):
    """Loads name/type/config/credencial_ref/criado_por and applies the admin-or-creator edge.

    404 if missing; generic 403 if the actor is neither the creator nor an admin (same rule as delete)."""
    c = (
        await conn.execute(
            text(
                "SELECT nome, tipo, config, credencial_ref, criado_por FROM conexoes WHERE id = :id"
            ),
            {"id": conexao_id},
        )
    ).first()
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    if ator_perfil != "admin" and c.criado_por != ator_id:
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)
    return c


async def renomear(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    nome: str,
    ator_id: uuid.UUID,
    ator_perfil: str | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Renames the connection (updates ONLY the `nome` column; audited). Edge: creator or admin.

    400 if the name is empty; 409 if the name is already in use (`nome` is UNIQUE). No secret in the diff."""
    novo = nome.strip()
    if not novo:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)
    c = await _carregar_meta_ou_403(conn, conexao_id, ator_id, ator_perfil)
    if novo == c.nome:
        return {"id": str(conexao_id), "nome": novo}
    em_uso = (
        await conn.execute(
            text("SELECT 1 FROM conexoes WHERE nome = :n AND id <> :id"),
            {"n": novo, "id": conexao_id},
        )
    ).first()
    if em_uso is not None:
        raise ErroAplicacao(
            codigo="nome_em_uso", mensagem="Connection name already in use.", status=409
        )
    await conn.execute(
        text("UPDATE conexoes SET nome = :n WHERE id = :id"), {"n": novo, "id": conexao_id}
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="conexao",
        recurso_id=conexao_id,
        ator_id=ator_id,
        antes={"nome": c.nome},
        depois={"nome": novo},
        request_id=request_id,
        ip=ip,
    )
    return {"id": str(conexao_id), "nome": novo}


async def duplicar(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    nome: str,
    ator_id: uuid.UUID,
    ator_perfil: str | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> uuid.UUID:
    """NEW connection with the type, config and the SAME credential as another, without anyone seeing
    or retyping the secret: the encrypted row is copied inside the vault (`vault.copiar`).

    Edge: creator or admin of the SOURCE (same as edit/delete). Being able to see the connection is
    not enough: the copy belongs to whoever duplicates, and giving ownership of a credential to
    someone who could only use it is a privilege escalation. The copy is born WITHOUT
    `permitir_escrita`: enabling writes is an audited admin action of its own, and duplicating must
    not become a shortcut to a writable connection. Audit trail: `criar` of the new one, with
    `duplicada_de` and no secret, in the same transaction."""
    novo = nome.strip()
    if not novo:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)
    c = await _carregar_meta_ou_403(conn, conexao_id, ator_id, ator_perfil)
    em_uso = (
        await conn.execute(text("SELECT 1 FROM conexoes WHERE nome = :n"), {"n": novo})
    ).first()
    if em_uso is not None:
        raise ErroAplicacao(
            codigo="nome_em_uso", mensagem="Connection name already in use.", status=409
        )
    ref = await vault.copiar(conn, uuid.UUID(str(c.credencial_ref))) if c.credencial_ref else None
    cid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO conexoes "
            "(id, nome, tipo, config, credencial_ref, criado_por, ativo, permitir_escrita) "
            "VALUES (:id, :n, :t, :c, :r, :por, true, false)"
        ),
        {
            "id": cid,
            "n": novo,
            "t": c.tipo,
            "c": _json(c.config),
            "r": str(ref) if ref else None,
            "por": ator_id,
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="conexao",
        recurso_id=cid,
        ator_id=ator_id,
        antes=None,
        depois={
            "nome": novo,
            "tipo": c.tipo,
            "config": c.config,
            "duplicada_de": str(conexao_id),
            "credencial": "copiada" if ref else None,  # NO secret
        },
        request_id=request_id,
        ip=ip,
    )
    return cid


async def editar(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    config: dict,
    segredo: str | None,
    ator_id: uuid.UUID,
    ator_perfil: str | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Edits the connection: updates `config` (NEVER with a secret) and, if a new `segredo` arrives,
    rotates the credential in the vault (Fernet), keeping the current credential when `segredo` is omitted.

    Edge: creator or admin (same rule as delete). Audited, NO secret in the diff.

    Credential policy:
      - `segredo` omitted (None)         -> keeps the current credential (`credencial_ref` unchanged).
      - `segredo` given, a ref exists    -> rotates the value in the vault (same reference).
      - `segredo` given, no ref          -> stores a new secret and points to it.
    """
    if "senha" in config or "password" in config or "segredo" in config:
        raise ErroAplicacao(
            codigo="segredo_em_config", mensagem="The config must not contain a secret.", status=400
        )
    c = await _carregar_meta_ou_403(conn, conexao_id, ator_id, ator_perfil)
    _validar_config(c.tipo, config)

    cred_acao: str | None = None
    if segredo is not None:
        if c.credencial_ref:
            await vault.rotacionar(conn, uuid.UUID(c.credencial_ref), segredo)
            cred_acao = "rotacionada"
        else:
            ref = await vault.guardar(conn, segredo)
            await conn.execute(
                text("UPDATE conexoes SET credencial_ref = :r WHERE id = :id"),
                {"r": str(ref), "id": conexao_id},
            )
            cred_acao = "adicionada"

    await conn.execute(
        text("UPDATE conexoes SET config = CAST(:c AS jsonb) WHERE id = :id"),
        {"c": _json(config), "id": conexao_id},
    )
    _esquecer_backend(conexao_id)
    depois: dict = {"config": config}  # NO secret
    if cred_acao:
        depois["credencial"] = cred_acao
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="conexao",
        recurso_id=conexao_id,
        ator_id=ator_id,
        antes={"config": c.config},
        depois=depois,  # NO secret, neither old nor new
        request_id=request_id,
        ip=ip,
    )


async def definir_escrita(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    permitir: bool,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Turns the connection's `permitir_escrita` on/off (lets effect nodes use it as a destination).

    Admin only (edge in the route, `SoAdmin`). 404 if missing. Audited with a before/after diff;
    a no-op (value already equal) writes no record and does not fail. No secret in the diff, only the flag."""
    c = (
        await conn.execute(
            text("SELECT permitir_escrita FROM conexoes WHERE id = :id"), {"id": conexao_id}
        )
    ).first()
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    atual = bool(c.permitir_escrita)
    if atual == permitir:
        return {"id": str(conexao_id), "permitir_escrita": atual}
    await conn.execute(
        text("UPDATE conexoes SET permitir_escrita = :p WHERE id = :id"),
        {"p": permitir, "id": conexao_id},
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="conexao",
        recurso_id=conexao_id,
        ator_id=ator_id,
        antes={"permitir_escrita": atual},
        depois={"permitir_escrita": permitir},
        request_id=request_id,
        ip=ip,
    )
    return {"id": str(conexao_id), "permitir_escrita": permitir}


# --- Writes (effect nodes). They resolve the credential in the vault and RE-CHECK the flag on every call. ---


async def _carregar_escrita(conn: AsyncConnection, conexao_id: uuid.UUID):
    return (
        await conn.execute(
            text(
                "SELECT tipo, config, credencial_ref, permitir_escrita FROM conexoes WHERE id = :id"
            ),
            {"id": conexao_id},
        )
    ).first()


async def resolver_escrita(
    conn: AsyncConnection, conexao_id: uuid.UUID
) -> tuple[str, dict, str | None]:
    """Resolves (type, config, password) of a WRITE connection (escrita_sql). 404 if missing;
    400 if the type cannot write (postgres/sqlserver only); 403 `conexao_sem_escrita` without the flag.
    The secret is resolved only here, at runtime, and never echoed."""
    c = await _carregar_escrita(conn, conexao_id)
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    if c.tipo not in TIPOS_ESCRITA_SQL:
        raise ErroAplicacao(
            codigo="conexao_tipo_sem_escrita",
            mensagem=f"Connection of type '{c.tipo}' does not support writes (use postgres or sqlserver).",
            status=400,
        )
    if not c.permitir_escrita:
        raise ErroAplicacao(
            codigo="conexao_sem_escrita",
            mensagem="Connection is not enabled for writes.",
            status=403,
        )
    senha = await vault.resolver(conn, uuid.UUID(c.credencial_ref)) if c.credencial_ref else None
    return c.tipo, c.config, senha


async def resolver_email(conn: AsyncConnection, conexao_id: uuid.UUID) -> tuple[dict, str | None]:
    """Resolves (config, password) of an `smtp` connection enabled for writes (`email` node).
    404 / 400 (type) / 403 `conexao_sem_escrita`. Secret resolved only at runtime, never echoed.

    Reuses `permitir_escrita` on purpose: sending e-mail is an external effect just like writing to
    a database, and a second flag would only add one more authorisation path to forget to check."""
    c = await _carregar_escrita(conn, conexao_id)
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    if c.tipo not in TIPOS_EMAIL:
        raise ErroAplicacao(
            codigo="conexao_tipo_sem_email",
            mensagem=f"Connection of type '{c.tipo}' cannot send e-mail (use smtp).",
            status=400,
        )
    if not c.permitir_escrita:
        raise ErroAplicacao(
            codigo="conexao_sem_escrita",
            mensagem="Connection is not enabled for writes.",
            status=403,
        )
    senha = await vault.resolver(conn, uuid.UUID(c.credencial_ref)) if c.credencial_ref else None
    return c.config, senha


async def resolver_http(conn: AsyncConnection, conexao_id: uuid.UUID) -> tuple[dict, str | None]:
    """Resolves (config, password) of an `api_rest` connection enabled for writes (http node).
    404 / 400 (type) / 403 `conexao_sem_escrita`. Secret resolved only at runtime."""
    c = await _carregar_escrita(conn, conexao_id)
    if c is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Connection not found.", status=404)
    if c.tipo != "api_rest":
        raise ErroAplicacao(
            codigo="conexao_tipo_sem_http",
            mensagem=f"Connection of type '{c.tipo}' is not a REST API.",
            status=400,
        )
    if not c.permitir_escrita:
        raise ErroAplicacao(
            codigo="conexao_sem_escrita",
            mensagem="Connection is not enabled for writes.",
            status=403,
        )
    senha = await vault.resolver(conn, uuid.UUID(c.credencial_ref)) if c.credencial_ref else None
    return c.config, senha


# The WRITE itself (table mode / DML) is triggered by execution via `no_escrita_sql.executar_escrita`
# (projects the rows, then calls the connector directly), NOT by a wrapper here. This module's role in
# writing is only `resolver_escrita`/`resolver_http` (resolve the credential + re-check the flag).


async def rotacionar_credencial(
    conn: AsyncConnection,
    *,
    conexao_id: uuid.UUID,
    novo_segredo: str,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    c = await _carregar(conn, conexao_id)
    if c is None or not c.credencial_ref:
        raise ErroAplicacao(
            codigo="nao_encontrado", mensagem="Connection has no credential.", status=404
        )
    await vault.rotacionar(conn, uuid.UUID(c.credencial_ref), novo_segredo)
    _esquecer_backend(conexao_id)
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="conexao",
        recurso_id=conexao_id,
        ator_id=ator_id,
        antes={"credencial": "rotacionada"},
        depois={"credencial": "rotacionada"},  # NO secret
        request_id=request_id,
        ip=ip,
    )
