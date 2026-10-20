# Glossary

Canonical vocabulary. Use these terms consistently in code, UI, comments and documentation. Code
identifiers are mostly Portuguese (`pipelines`, `execucoes`, `nos`, `arestas`...); the English term
is given first.

## Platform

- **Studio** - the authoring and operations surface: graphs, connections, cubes, views, pages, runs,
  schedules, ingestion.
- **Portal** - the read-only consumption surface for operators: catalogue of pages, allowed filters,
  report generation.
- **Pipeline / graph** (`pipelines`) - connected nodes that transform data from sources to an output.
- **Node** (`nos`) - a step of the graph: `fonte`, `sql`, `guiada`, `comparacao`, `python`, `saida`
  and the effect nodes `escrita_sql`, `http`, `email`, `ingestao`.
- **Edge** (`arestas`) - a dependency between two nodes.
- **Preview** - instant recomputation of a node over a sample in a session DuckDB; no Temporal.
- **Snapshot** - the full output of a node persisted (Parquet + metadata) during a production run.
- **Run** (`execucoes`) - one execution of a pipeline version. States: `executando`, `concluida`,
  `falha`.
- **Chain** (`cadeia_id`) - a trigger of a pipeline *with* its upstream pipelines: several runs
  sharing a chain id; the **root** is the requested one, the others are **upstream** members.
- **Re-execution** (`reexecucao_de`) - a new run linked to an original one, optionally starting at a
  node. Effects repeat.
- **In-flight / duplicate run** - same pipeline and parameters still `executando`; the next trigger
  gets `409 execucao_em_andamento`. An **orphan** is a run stuck in `executando` with a dead
  workflow; the **reaper** closes it.
- **Fingerprint** - identity of a node result; equal fingerprint means the snapshot is reused.
- **Runtime parameter** (`:name`) - value given at trigger time, always bound; `obrigatorio` makes it
  mandatory; `segredo: true` redacts it from everything persisted.
- **Function library** - versioned set of DuckDB macros reused by nodes; pinned per run.
- **Vault** - encrypted credential store, separate from metadata, resolved only in the worker.
- **Audit trail** (`auditoria`) - append-only record of every write.
- **Group of a pipeline** (`grupo`) - optional classification: `load`, `reference` or `export`.
  Independent of **layer** (`camada`: bronze/silver/gold) and of **system** (`sistema`, who may see
  it).

## Modelling and consumption

- **Cube** - semantic model published from a pipeline output: dimensions, measures, hierarchies,
  formats.
- **Dimension** - an analysis axis. **Measure** - a named, governed calculation defined once.
- **View** - saved pivot configuration. **Page** - composition of blocks that references the model.
  **Block** - unit of a page (pivot, chart, KPI card, text).
- **Stamped report** - export of a page carrying a stamp (time, run, version, filters, user).
- **Report layout / export** - declarative `.xlsx` layout over a node output, and the immutable
  artifact generated for a (run, node, layout, version) tuple.

## Lake

- **Landing** - raw payload as received, immutable. **Bronze** - Parquet, all `VARCHAR`, faithful to
  the origin. **Silver** - bronze typed 1:1, versioned per run (Iceberg table or Parquet + pointer).
  **Gold** - conformed product.
- **Source / contract** - declarative description (URL pattern, format, encoding, cadence, grain) of
  an external origin. The **connector** is per access class (`http_file`, `rest_api`, `odata`,
  `portal_download`), never per source.
- **Slice** - one request to the origin (a day, a month, a file). **Batch** - what landed in bronze
  for a slice, unique per `(source, key, sha256)`. **Watermark** - how far the incremental load got;
  advances only without quarantine. **Quarantine** - slice with invalid data (layout, parse) set
  aside; infrastructure failures are not quarantine.
- **Grain** - what ONE row of a table is (`contrato.grao`); a blocking guard checks it at publication.
- **`_atual.json`** - mutable pointer to the current version of a scope; in a partitioned dataset it
  is the current index per scope. **`_indice.json`** - immutable index of one run.
- **Environment namespace** - `dev` / `prd` prefix at the root of the gold layer.

## Profiles

- **Administrator** - users, permissions, connections, infrastructure.
- **Specialist** - authors and governs in the Studio.
- **Operator** - consumes in the Portal and generates reports.
- **Auditor** - reads the audit trail and traces numbers to their origin.
- **Service** (`servico`) - API-token-only account that runs and follows the pipelines it was granted.

## Terms to avoid

- "dashboard" for what we call a **page**.
- Temporal vocabulary (workflow, activity, task queue) in the UI.
- "formula" for a **measure**.
