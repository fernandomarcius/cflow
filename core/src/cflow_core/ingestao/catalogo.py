"""Example seed contracts for external sources.

These are **illustrative examples**, not real data sources: every URL points at `example.com`
and every dataset name is neutral. They show how to describe each access class
(`rest_api`, `odata`, `http_file`, `portal_download`) and the optional blocks (`janela`,
`formato`, `grao`) so you can copy one as a starting point for your own source.

A contract is **data**, not code. Once registered through the API it is edited through the API;
changing this file changes nothing in a running deployment, and that is by design.

Cadence is a monthly reload. A source that publishes every day is still captured day by day:
the monthly run starts at the watermark and rebuilds the whole window, one slice per day.
Only sources with **no history at the origin** (a fixed URL that is overwritten in place) lose
the gap: for those only the snapshot of the day the job runs can be captured.
"""

from __future__ import annotations

# --- rest_api: window passed as query parameters ------------------------------------------------
# Maximum window of 10 years per request, no authentication. One source PER series: the `dataset`
# is the path in the lake, and mixing series in one folder would force every consumer to filter
# by a code that is not even in the payload.

RATES_DAILY = {
    "classe": "rest_api",
    "dataset": "rates_daily",
    "descricao": "Example: daily reference rate, one point per day",
    "cadencia": "mensal",
    "janela": {"tipo": "periodo", "inicio": "2010-01-01", "passo_max_dias": 3650},
    "acesso": {
        "url_padrao": "https://example.com/api/series/{serie}/data",
        "variaveis": {"serie": "rate-01"},
        "params": {"format": "json"},
        "param_inicio": "from",
        "param_fim": "to",
        "formato_data": "%d/%m/%Y",
    },
    "formato": {"tipo": "json", "encoding": "utf-8"},
    "schema_esperado": ["date", "value"],
    "limites": {"concorrencia": 4, "timeout_segundos": 120, "tentativas": 3},
}

# --- odata: paginated service with the window as a filter ---------------------------------------

FX_QUOTES = {
    "classe": "odata",
    "dataset": "fx_quotes",
    "descricao": "Example: currency quotes per period (OData)",
    "cadencia": "mensal",
    # Monthly slice: the filter receives the period, so each month is a closed batch.
    "janela": {"tipo": "mensal", "inicio": "2015-01"},
    "acesso": {
        "url_padrao": "https://example.com/odata/v1/Quotes",
        "params": {"$format": "json"},
        # The date is part of the OData filter expression, quotes included.
        "params_janela": {
            "$filter": "QuoteDate ge '{inicio:%Y-%m-%d}' and QuoteDate le '{fim:%Y-%m-%d}'",
        },
        "top": 1000,
        "max_paginas": 50,
        "campo_itens": "value",
    },
    "formato": {"tipo": "json", "encoding": "utf-8"},
    "schema_esperado": ["buy", "sell", "quote_time"],
    "limites": {"concorrencia": 2, "timeout_segundos": 120},
}

# --- http_file: a ZIP with a CSV inside ---------------------------------------------------------
# The encoding is declared: decoding as UTF-8 "works" for most rows and corrupts exactly the ones
# with accents. The yearly file is republished on every competence, so the content hash is what
# prevents re-downloading the history.

REGISTRY_MONTHLY = {
    "classe": "http_file",
    "dataset": "registry_monthly",
    "descricao": "Example: yearly registry file, republished every month",
    "cadencia": "mensal",
    "janela": {"tipo": "anual", "inicio": "2019"},
    "acesso": {
        "url_padrao": "https://example.com/files/registry_{ano}.zip",
        "condicional": True,
    },
    "formato": {
        "tipo": "csv",
        "encoding": "latin-1",
        "delimitador": ";",
        "compressao": "zip",
        "arquivo_no_zip": r"registry_\d{4}\.csv$",
        # Free-text fields without quotes can split a row: tolerate it for this source only, and
        # let the manifest record what was dropped.
        "tolerar_linhas_invalidas": True,
    },
    "schema_esperado": ["entity_id", "reference_date", "delivered_at", "amount"],
    "limites": {"concorrencia": 2, "timeout_segundos": 300},
}

# --- http_file: one file per business day, no date column inside --------------------------------
# A file per day: `daily_{yy}{mm}{dd}.txt`. A day without a file does not exist at the origin and
# becomes a 404, which the engine counts as `nao_publicado` (not a failure, no dead-letter).
# `fatia_e_competencia`: the file carries NO date column, so the slice key is projected as the
# `competencia` column. Without it, many days stack in one table where identical rows are not
# duplicates but the same item on different days.

REGISTRY_DAILY = {
    "classe": "http_file",
    "dataset": "registry_daily",
    "descricao": "Example: daily file without a date column (the slice is the competence)",
    "cadencia": "mensal",
    "janela": {
        "tipo": "diaria",
        "inicio": "2026-01-01",
        "atraso_dias": 1,
        "fatia_e_competencia": True,
    },
    "acesso": {
        "url_padrao": "https://example.com/files/daily_{ano_curto}{mes}{dia}.txt",
        "variaveis": {},
        "headers": {"User-Agent": "cFlow/1.0 (+https://example.com)"},
        "condicional": True,
    },
    "formato": {
        "tipo": "csv",
        "encoding": "latin-1",
        "delimitador": "@",
        # Two title lines before the column header.
        "pular_linhas": 2,
    },
    "schema_esperado": ["code", "name", "rate"],
    "limites": {"concorrencia": 1, "timeout_segundos": 120},
}

# --- portal_download: links discovered on an index page -----------------------------------------
# The index page is a fixed URL, so the source does not depend on the slice: only the most recent
# slice is kept (the origin overwrites the files in place and keeps no history).

REPORTS_PORTAL = {
    "classe": "portal_download",
    "dataset": "reports_portal",
    "descricao": "Example: report files linked from an index page",
    "cadencia": "mensal",
    "janela": {"tipo": "diaria", "inicio": "2026-01-01", "atraso_dias": 1},
    "acesso": {
        "pagina_indice": "https://example.com/reports/index.html",
        "padrao_link": r"report_.*\.csv$",
        "max_arquivos": 20,
        "headers": {"User-Agent": "cFlow/1.0 (+https://example.com)"},
        "condicional": True,
    },
    "formato": {"tipo": "csv", "encoding": "utf-8", "delimitador": ","},
    "limites": {"concorrencia": 1, "timeout_segundos": 120},
}

CATALOGO = {
    c["dataset"]: c
    for c in (
        RATES_DAILY,
        FX_QUOTES,
        REGISTRY_MONTHLY,
        REGISTRY_DAILY,
        REPORTS_PORTAL,
    )
}

# --- Declared grain: what ONE ROW of each table is ----------------------------------------------
#
# Only a key that was MEASURED against real data belongs here. Guessing a key is worse than not
# declaring one: a wrong key yields a plausible but false duplicate count. Everything else is born
# `desconhecido` (the absence is normalised in `contrato.grao`), a deliberately noisy state rather
# than an "everything is fine".
#
#   rates_daily      -> (date): one point per day; the payload is (date, value) and nothing else.
#   registry_monthly -> (entity_id, reference_date): the file is yearly and resubmitted on every
#                       competence, so the reference date is part of the identity. The same
#                       record can still be redelivered later; `desempate` keeps the latest.
#   registry_daily   -> (code, competencia): the file has no date column, the competence comes
#                       from the projected slice (`janela.fatia_e_competencia`).
GRAOS_DECLARADOS: dict[str, dict] = {
    "rates_daily": {"chaves": ("date",)},
    "registry_monthly": {
        "chaves": ("entity_id", "reference_date"),
        # When a group differs only in `delivered_at`, the origin redelivered the same record:
        # `desc` keeps the most recent delivery. A residual tie (two deliveries on the same day)
        # is not resolved here and keeps blocking publication, which is the right behaviour.
        "desempate": {
            "colunas": [{"coluna": "delivered_at", "ordem": "desc"}],
            "motivo": "the origin redelivers the same record later; the latest delivery wins",
        },
    },
    "registry_daily": {"chaves": ("code", "competencia")},
}

# `KeyError` on purpose: renaming a dataset without reviewing its grain must break the import,
# not leave a measured declaration pointing at a dataset that no longer exists.
for _dataset, _decl in GRAOS_DECLARADOS.items():
    CATALOGO[_dataset]["grao"] = {
        "estado": "declarado",
        "chaves": list(_decl["chaves"]),
        "motivo": None,
        **({"desempate": _decl["desempate"]} if "desempate" in _decl else {}),
    }

# Display name: the contract's own description, which is already written for humans. Two lists to
# keep in sync is one that goes stale.
NOMES = {ds: c.get("descricao", ds) for ds, c in CATALOGO.items()}
