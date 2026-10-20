"""`ingestao` node - external ingestion as a node of the Studio graph.

**What this node returns is the LOG, not the data.** The ingested data goes to the lake (landing ->
bronze) and is read by the `fonte lake` node; this node's snapshot is one row per slice - key,
window, outcome, rows, reason. That is not an implementation detail, it is the reason the node
exists: whoever looks at an ingestion run wants to know *what happened to each slice*, which is
exactly the question the aggregate summary ("22 batches") never answered. Returning millions of
rows as the snapshot would duplicate the dataset inside the run Parquet for nobody to read.

**The contract remains data, not graph.** The node stores `fonte_id` and, at most, the window; URL,
format, encoding and cadence stay in `ingestao_fontes.contrato`, maintained by an admin. What became
graph is the ORCHESTRATION - which brings for free the run history, the detail screen, schedule
reuse and the declared dependency (`fonte` with `origem:"pipeline"`, which `pipelines/dag.py`
already resolves by running upstream first).
"""

from __future__ import annotations

from datetime import date

from ..errors import ErroAplicacao
from ..ingestao.contrato import fatias as recortar_fatias
from ..ingestao.service import resolver_janela

# Node snapshot columns. Order designed for the run screen grid: the slice identifies the row, the
# outcome is what one looks for, and the reason sits next to the outcome that requires it.
COLUNAS_LOG: tuple[tuple[str, str], ...] = (
    ("chave_lote", "VARCHAR"),
    ("janela_inicio", "DATE"),
    ("janela_fim", "DATE"),
    ("desfecho", "VARCHAR"),
    ("linhas", "BIGINT"),
    ("bytes", "BIGINT"),
    ("tentativa", "INTEGER"),
    ("duracao_ms", "INTEGER"),
    ("etapa", "VARCHAR"),
    ("motivo", "VARCHAR"),
    ("bronze_uri", "VARCHAR"),
)


def criar_tabela_log(duck, no_id: str) -> None:
    colunas = ", ".join(f'"{n}" {t}' for n, t in COLUNAS_LOG)
    duck.execute(f'DROP TABLE IF EXISTS "{no_id}"')
    duck.execute(f'CREATE TABLE "{no_id}" ({colunas})')


def linha_de(fatia: dict) -> list:
    """One log row from the `ingestao_fatias` record (already with the quarantine reason attached
    by `repositorio.listar_fatias`).

    `motivo` unifies two origins because, for the reader, it is the same column: an INFRA failure
    comes in `erro` (written by the slice's own log) and a DATA failure comes from quarantine.
    Keeping two mostly-empty columns would force looking at both to find the only filled one."""
    return [
        fatia.get("chave_lote"),
        fatia.get("janela_inicio"),
        fatia.get("janela_fim"),
        fatia.get("desfecho"),
        fatia.get("linhas"),
        fatia.get("bytes"),
        fatia.get("tentativa"),
        fatia.get("duracao_ms"),
        fatia.get("quarentena_etapa"),
        fatia.get("erro") or fatia.get("quarentena_mensagem"),
        fatia.get("bronze_uri"),
    ]


def inserir_log(duck, no_id: str, fatias: list[dict]) -> None:
    marcas = ", ".join("?" for _ in COLUNAS_LOG)
    for f in fatias:
        duck.execute(f'INSERT INTO "{no_id}" VALUES ({marcas})', linha_de(f))


def fonte_id_de(cfg: dict, no_id: str) -> str:
    fid = (cfg or {}).get("fonte_id")
    if not fid:
        raise ErroAplicacao(
            codigo="ingestao_sem_fonte",
            mensagem=f"Node '{no_id}' (ingestion) requires config.fonte_id.",
            status=400,
        )
    return str(fid)


def janela_de(cfg: dict) -> tuple[date | None, date | None]:
    """Window declared on the node. Absent = incremental (from watermark to today), which is the
    common case and the only one the scheduled reload uses."""

    def _d(v) -> date | None:
        if not v:
            return None
        return date.fromisoformat(str(v))

    return _d((cfg or {}).get("inicio")), _d((cfg or {}).get("fim"))


def plano_dry_run(*, fonte: dict, watermark: dict | None, cfg: dict) -> dict:
    """What ingestion WOULD do, without touching the network: effective window and the slices that
    would be requested.

    Previewing a side-effect node must not trigger the effect - here that matters even more: each
    slice is a request to a public origin, and a preview that really downloaded would get the IP
    blocked at every keystroke in the editor. The slicing uses the SAME function as the run
    (`contrato.fatias`), so the number of slices shown is the number that will run."""
    contrato = fonte["contrato"]
    inicio, fim = janela_de(cfg)
    ini, f = resolver_janela(contrato, watermark, inicio, fim)
    lista = recortar_fatias(contrato, ini, f)
    return {
        "dataset": fonte["dataset"],
        "classe": contrato.get("classe"),
        "janela": {"inicio": ini.isoformat(), "fim": f.isoformat()},
        "fatias": [
            {"chave": x.chave, "inicio": x.inicio.isoformat(), "fim": x.fim.isoformat()}
            for x in lista
        ],
        "watermark": (watermark or {}).get("chave_lote"),
    }
