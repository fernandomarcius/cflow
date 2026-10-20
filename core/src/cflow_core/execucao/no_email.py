"""`email` effect node: pure ASSEMBLY (what the preview shows) separated from the EFFECT (sending).
Same invariant as the other effect nodes: the message the dry-run shows is built by the SAME
`montar_plano` that the execution sends; diverging is a bug.

- `montar_plano(cfg, ...)`: pure, no network. Renders subject/body, resolves recipients and
  materialises attachments as bytes. Used in preview (dry-run) and in execution.
- `executar_email(conector, ...)`: the effect. Delivers to the `ConectorEmail` (credential and flag
  already resolved/re-checked by the caller).

The node knows nothing about SMTP (it talks to the `ConectorEmail` protocol) and data access goes
through `FonteDados`: in preview and execution the data is in DuckDB, but tests inject a dict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Protocol

from ..conexoes.conectores.base import AnexoEmail, MensagemEmail
from ..errors import ErroAplicacao
from . import email_anexos as anexos
from .email_template import TABELA_LIMITE_PADRAO, Contexto, renderizar

MODOS = ("unico", "por_linha")
TOLERANCIAS = ("abortar", "continuar")

# Schema of the SUMMARY table the node materialises as a snapshot: 1 row per e-mail. Single source
# of the format, reused by execution and dry-run so both schemas stay identical.
COLUNAS_RESUMO = (
    "destinatarios VARCHAR, assunto VARCHAR, anexos INTEGER, bytes_anexos BIGINT, "
    "status VARCHAR, duracao_ms BIGINT, erro VARCHAR"
)


def criar_tabela_resumo(duck, nome: str) -> None:
    """Create (recreating) the node summary table in DuckDB; same schema in preview and execution."""
    duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
    duck.execute(f'CREATE TABLE "{nome}" ({COLUNAS_RESUMO})')


class FonteDados(Protocol):
    """Where the node reads a predecessor's data from. Real implementation: `DadosDuckDB`."""

    def tabela(self, no_id: str) -> tuple[list[str], list[dict]]:
        """Return `(columns, rows)` of the already computed node."""
        ...

    def parquet(self, no_id: str) -> bytes:
        """Return the node serialised as Parquet (attachment `formato: 'parquet'`)."""
        ...


class DadosDuckDB:
    """Adapter from the session/execution DuckDB to `FonteDados`. The Parquet comes out of the SAME
    engine that writes snapshots, so attachment and snapshot cannot disagree on types."""

    def __init__(self, duck) -> None:
        self._duck = duck

    def tabela(self, no_id: str) -> tuple[list[str], list[dict]]:
        cur = self._duck.execute(f'SELECT * FROM "{no_id}"')
        colunas = [d[0] for d in cur.description]
        return colunas, [dict(zip(colunas, linha, strict=True)) for linha in cur.fetchall()]

    def parquet(self, no_id: str) -> bytes:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="cflow_email_") as td:
            destino = Path(td) / f"{no_id}.parquet"
            self._duck.execute(f"COPY \"{no_id}\" TO '{destino}' (FORMAT parquet)")
            return destino.read_bytes()


@dataclass
class PlanoEmail:
    """What WOULD be sent. `mensagens` already carries the rendered body and attachments as bytes."""

    modo: str
    mensagens: list[MensagemEmail] = field(default_factory=list)
    nao_resolvidos: list[str] = field(default_factory=list)
    contagem_entrada: int = 0
    host_destino: str = ""
    remetente: str = ""

    @property
    def total(self) -> int:
        return len(self.mensagens)


def enderecos(texto: str) -> list[str]:
    """Split on `;`/`,`, trim, drop duplicates and discard anything that is not an address. Tolerant
    on purpose: real address lists come with stray spaces and mixed separators."""
    saida: list[str] = []
    for parte in str(texto or "").replace(",", ";").split(";"):
        _, addr = parseaddr(parte.strip())
        if addr and "@" in addr and " " not in addr and addr not in saida:
            saida.append(addr)
    return saida


def _ctx(
    cfg: dict, colunas: list[str], linhas: list[dict], linha: dict | None, params: dict
) -> Contexto:
    return Contexto(
        colunas=colunas,
        linhas=linhas,
        linha=linha,
        params=params or {},
        html=(cfg.get("corpo_tipo") or "html") != "texto",
        tabela_limite=int(cfg.get("tabela_limite") or TABELA_LIMITE_PADRAO),
    )


def _anexos_da_mensagem(
    cfg: dict,
    dados: FonteDados,
    entrada: str | None,
    ctx: Contexto,
    linhas_entrada: list[dict],
    colunas_entrada: list[str],
) -> list[AnexoEmail]:
    """Materialise the attachments of ONE message.

    `origem: 'coluna'` in `unico` mode yields one attachment PER input row (the previous node built
    N files and the e-mail carries all of them); in `por_linha` mode, one attachment from the
    current row."""
    saida: list[AnexoEmail] = []
    for cfg_anexo in cfg.get("anexos") or []:
        origem = cfg_anexo.get("origem") or "dados"
        if origem == "coluna":
            alvos = [ctx.linha] if ctx.linha is not None else linhas_entrada
            saida.extend(anexos.de_coluna(cfg_anexo, linha) for linha in alvos)
            continue
        no_dados = cfg_anexo.get("de") or entrada
        if not no_dados:
            raise ErroAplicacao(
                codigo="email_anexo_sem_no",
                mensagem="Data attachment requires an input node (connect a node or set 'de').",
                status=400,
            )
        if no_dados == entrada:
            cols, lins = colunas_entrada, linhas_entrada
        else:
            cols, lins = dados.tabela(no_dados)
        formato = cfg_anexo.get("formato") or "csv"
        nome_bruto, _ = renderizar(cfg_anexo.get("nome") or f"{no_dados}.{formato}", ctx)
        saida.append(
            anexos.de_dados(
                cfg_anexo, nome_bruto, cols, lins, parquet_bytes=lambda n=no_dados: dados.parquet(n)
            )
        )
    return saida


def montar_plano(
    cfg: dict,
    *,
    dados: FonteDados,
    entrada: str | None,
    params: dict | None = None,
    config_conexao: dict | None = None,
) -> PlanoEmail:
    """Build the messages (pure, no network). `entrada` = predecessor id (None = e-mail with no data).

    Fails LOUDLY on 0 rows without `permitir_vazio`: a report that goes out with no rows is the
    classic silent failure, as it reaches the reader looking delivered."""
    modo = cfg.get("modo") or "unico"
    if modo not in MODOS:
        raise ErroAplicacao(
            codigo="email_modo",
            mensagem=f"Invalid mode '{modo}' (use {' or '.join(MODOS)}).",
            status=400,
        )
    colunas, linhas = dados.tabela(entrada) if entrada else ([], [])
    if entrada and not linhas and not cfg.get("permitir_vazio", False):
        raise ErroAplicacao(
            codigo="email_vazio",
            mensagem="0 input rows and permitir_vazio=false: no e-mail was built.",
            status=400,
        )
    if modo == "por_linha" and not entrada:
        raise ErroAplicacao(
            codigo="email_sem_entrada",
            mensagem="Mode 'por_linha' requires exactly one connected input.",
            status=400,
        )

    conexao = config_conexao or {}
    alvos: list[dict | None] = list(linhas) if modo == "por_linha" else [None]
    mensagens: list[MensagemEmail] = []
    nao_resolvidos: list[str] = []
    for linha in alvos:
        ctx = _ctx(cfg, colunas, linhas, linha, params or {})
        assunto, nr1 = renderizar(cfg.get("assunto") or "", ctx)
        corpo, nr2 = renderizar(cfg.get("corpo") or "", ctx)
        para_txt, nr3 = renderizar(cfg.get("para") or "", ctx)
        cc_txt, nr4 = renderizar(cfg.get("cc") or "", ctx)
        cco_txt, nr5 = renderizar(cfg.get("cco") or "", ctx)
        for nome in (*nr1, *nr2, *nr3, *nr4, *nr5):
            if nome not in nao_resolvidos:
                nao_resolvidos.append(nome)
        para = enderecos(para_txt)
        if not para:
            raise ErroAplicacao(
                codigo="email_sem_destinatario",
                mensagem="No valid recipient in 'para' (check the address/placeholder).",
                status=400,
            )
        mensagens.append(
            MensagemEmail(
                para=tuple(para),
                assunto=assunto,
                corpo=corpo,
                corpo_html=ctx.html,
                cc=tuple(enderecos(cc_txt)),
                cco=tuple(enderecos(cco_txt)),
                responder_para=(cfg.get("responder_para") or conexao.get("responder_para") or None),
                remetente_nome=(cfg.get("remetente_nome") or conexao.get("remetente_nome") or None),
                anexos=tuple(_anexos_da_mensagem(cfg, dados, entrada, ctx, linhas, colunas)),
            )
        )
    return PlanoEmail(
        modo=modo,
        mensagens=mensagens,
        nao_resolvidos=nao_resolvidos,
        contagem_entrada=len(linhas),
        host_destino=str(conexao.get("host") or ""),
        remetente=str(conexao.get("remetente") or conexao.get("usuario") or ""),
    )


def linha_resumo(
    mensagem: MensagemEmail, *, status: str, duracao_ms: int | None, erro: str | None
) -> list:
    """One summary-table row, in the ORDER of `COLUNAS_RESUMO` (the INSERT is positional)."""
    return [
        mensagem.destinatarios,
        mensagem.assunto,
        len(mensagem.anexos),
        mensagem.bytes_anexos,
        status,
        duracao_ms,
        erro,
    ]


async def executar_email(
    conector,
    config_conexao: dict,
    senha: str | None,
    plano: PlanoEmail,
    *,
    tolerancia: str = "abortar",
    timeout: int | None = None,
    tentativas: int | None = None,
) -> tuple[list[list], int]:
    """Fire the effect. Returns `(summary rows, how many e-mails went out)`.

    `tolerancia='abortar'` (default) stops at the first error and reports how many ALREADY went out:
    a partial effect is explicit, never silent. `'continuar'` records the failure in the summary row
    and moves on, so a batch of 500 e-mails does not die over one bad address."""
    if tolerancia not in TOLERANCIAS:
        tolerancia = "abortar"
    resumo: list[list] = []
    enviados = 0
    for i, mensagem in enumerate(plano.mensagens):
        inicio = time.monotonic()
        try:
            await conector.enviar(
                config_conexao, senha, mensagem, timeout=timeout, tentativas=tentativas
            )
        except ErroAplicacao as exc:
            duracao = int((time.monotonic() - inicio) * 1000)
            resumo.append(
                linha_resumo(mensagem, status="falha", duracao_ms=duracao, erro=exc.mensagem)
            )
            if tolerancia == "abortar":
                raise ErroAplicacao(
                    codigo="email_falha",
                    mensagem=(
                        f"E-mail #{i + 1} to {mensagem.destinatarios} failed: {exc.mensagem} "
                        f"{enviados} e-mail(s) already sent (partial effect)."
                    ),
                    status=400,
                ) from exc
            continue
        enviados += 1
        resumo.append(
            linha_resumo(
                mensagem, status="ok", duracao_ms=int((time.monotonic() - inicio) * 1000), erro=None
            )
        )
    return resumo, enviados
