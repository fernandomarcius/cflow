# Data model - metadata (PostgreSQL)

Schema of the platform **metadata**. Business data lives in the external sources and in the Parquet
snapshots/lake; here live definitions, versions, permissions, runs and the audit trail.

> The source of truth is `migrations/versions/` (`0001` ... `0042`). The SQL below is a consolidated
> reading, not DDL to execute.

Conventions:

- Primary keys are `uuid` (`gen_random_uuid()`).
- Timestamps are `timestamptz`, always UTC in the database; converted to the display time zone
  (`CFLOW_TZ`) at the API edge.
- `jsonb` for versioned definitions (graph, cube, view, page, layout): the JSON is the truth.
- Soft delete by status; never delete rows referenced by the audit trail.
- Two database roles: `cflow_owner` (owns the schema, runs migrations) and `cflow_app` (runtime;
  no DDL, and only `INSERT`/`SELECT` on `auditoria`).

## Tables outside the sections below

| Table | Migration | What it is |
|---|---|---|
| `sessoes`, `tentativas_login`, `tokens_recuperacao` | 0003 | server-side session (token hash), brute-force counter, password recovery |
| `configuracoes` | 0004 | operational key/value (for example `log_retencao_dias`) |
| `cofre_credenciais` | 0005 | Fernet-encrypted vault, separate from `conexoes` |
| `cubo_versoes`, `view_versoes` | 0010 | numbered versions of cubes and views |
| `biblioteca_funcoes`, `biblioteca_funcoes_versoes` | 0017 | versioned DuckDB macro libraries |
| `ingestao_fontes`, `_execucoes`, `_lotes`, `_watermarks`, `_quarentena` | 0029 | external ingestion; `_fatias` in 0032; one in-flight run per source in 0039 |
| `iceberg_tables`, `iceberg_namespace_properties` | 0030 | PyIceberg JDBC catalog |
| `relatorio_layouts`, `relatorio_layout_versoes`, `relatorio_exportacoes` | 0036 | report layer |
| `tokens_api` | 0040 | API tokens of the `servico` profile: SHA-256 + prefix, expiry, last use, revocation |

Columns added after the first design: `pipelines.sistema/camada/grupo/ativo`,
`conexoes.permitir_escrita`, `execucoes.*` (section 4), `execucao_nos.efeito_disparado`.

## 1. Identity and access

```sql
CREATE TYPE perfil AS ENUM ('admin', 'especialista', 'operador', 'auditor', 'servico');
CREATE TYPE nivel_permissao AS ENUM ('ver', 'executar', 'editar', 'administrar');
CREATE TYPE tipo_sujeito AS ENUM ('usuario', 'grupo');

CREATE TABLE usuarios (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text UNIQUE NOT NULL,
  nome          text NOT NULL,
  fonte_auth    text NOT NULL DEFAULT 'local',   -- 'local' | 'oidc'
  perfil        perfil NOT NULL,
  ativo         boolean NOT NULL DEFAULT true,
  mfa_habilitado boolean NOT NULL DEFAULT false,
  ultimo_acesso timestamptz,
  criado_em     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE grupos (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), nome text UNIQUE NOT NULL, descricao text);
CREATE TABLE usuarios_grupos (
  usuario_id uuid REFERENCES usuarios(id), grupo_id uuid REFERENCES grupos(id),
  PRIMARY KEY (usuario_id, grupo_id)
);

-- Permission per resource: pipeline | conexao | cubo | view | pagina
CREATE TABLE permissoes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sujeito_tipo  tipo_sujeito NOT NULL,
  sujeito_id    uuid NOT NULL,
  recurso_tipo  text NOT NULL,
  recurso_id    uuid NOT NULL,
  nivel         nivel_permissao NOT NULL,
  concedido_por uuid REFERENCES usuarios(id),
  criado_em     timestamptz NOT NULL DEFAULT now()
);
```

## 2. Connections and credentials

The secret is **never** stored in `conexoes`. `credencial_ref` points to the vault
(`cofre_credenciais`), resolved only inside the worker at run time.

```sql
CREATE TABLE conexoes (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nome           text UNIQUE NOT NULL,
  tipo           text NOT NULL,       -- sqlserver | postgres | api_rest | api_query | arquivo | objeto | smtp
  config         jsonb NOT NULL,      -- host, port, database, url, headers (NO secret)
  credencial_ref text,                -- pointer into the vault
  permitir_escrita boolean NOT NULL DEFAULT false,  -- gate for effect nodes that write
  criado_por     uuid REFERENCES usuarios(id),
  ativo          boolean NOT NULL DEFAULT true,
  criado_em      timestamptz NOT NULL DEFAULT now()
);
```

## 3. Pipelines and versions

```sql
CREATE TYPE status_versao AS ENUM ('rascunho', 'publicada', 'arquivada');

CREATE TABLE pipelines (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nome                text NOT NULL,
  descricao           text,
  dono_id             uuid REFERENCES usuarios(id),
  versao_publicada_id uuid,            -- FK to pipeline_versoes (null until first publication)
  camada              text,            -- bronze | silver | gold | NULL
  grupo               text,            -- load | reference | export | NULL
  sistema             boolean NOT NULL DEFAULT false,
  ativo               boolean NOT NULL DEFAULT true,
  criado_em           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE pipeline_versoes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_id   uuid NOT NULL REFERENCES pipelines(id),
  numero        int NOT NULL,
  definicao     jsonb NOT NULL,        -- { nos: [...], arestas: [...], parametros: [...] }
  status        status_versao NOT NULL DEFAULT 'rascunho',
  nota_mudanca  text,
  autor_id      uuid REFERENCES usuarios(id),
  criado_em     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (pipeline_id, numero)
);
```

## 4. Runs and snapshots

A run is orchestrated by Temporal, but **the control plane is PostgreSQL**: there is no workflow id
column; the id is derived (`exec-{run_id}` or `dag-{root}`). The node snapshot is a file
(`CFLOW_SNAPSHOT_DIR/<run_id>/<node_id>.parquet`); the table keeps the pointer.

```sql
CREATE TYPE status_execucao AS ENUM ('executando', 'concluida', 'falha');

CREATE TABLE execucoes (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_id         uuid NOT NULL REFERENCES pipelines(id),
  versao_numero       int NOT NULL,             -- the EXACT version that ran, never "the latest"
  status              status_execucao NOT NULL DEFAULT 'executando',
  erro                text,
  iniciada_por        uuid REFERENCES usuarios(id),
  iniciada_em         timestamptz NOT NULL DEFAULT now(),
  concluida_em        timestamptz,
  parametros          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- `segredo: true` already redacted
  pagina_id           uuid,               -- page the operator triggered it from (NULL = Studio)
  bibliotecas_versoes jsonb,              -- macro library pin (reproducibility)
  origem              text NOT NULL DEFAULT 'manual',  -- manual | agendado | reexecucao
  agendamento_id      uuid,
  cadeia_id           uuid,               -- same chained trigger
  cadeia_raiz         boolean NOT NULL DEFAULT false,  -- which run of the chain was REQUESTED
  reexecucao_de       uuid,
  reexecucao_desde_no text,
  chave_concorrencia  text                -- md5(pipeline_id || parameters), root only
);
-- The guarantee against duplicate runs belongs to the INDEX, not to a pre-check. Partial because only
-- the `executando` state is contended; upstream members keep a NULL key, which does not collide.
CREATE UNIQUE INDEX ux_execucoes_em_voo ON execucoes (chave_concorrencia) WHERE status = 'executando';

CREATE TABLE execucao_nos (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  execucao_id      uuid NOT NULL REFERENCES execucoes(id) ON DELETE CASCADE,
  no_id            text NOT NULL,
  status           status_execucao NOT NULL DEFAULT 'executando',
  linhas           bigint,
  hash             text,                 -- content hash (order independent)
  snapshot         text,                 -- Parquet path (NULL if snapshot disabled)
  duracao_ms       int,
  erro             text,
  concluido_em     timestamptz,
  fingerprint      text,                 -- result identity; equal => reuse
  reaproveitado    boolean NOT NULL DEFAULT false,
  efeito_disparado boolean,              -- effect guard per (run, node)
  iniciado_em      timestamptz,
  UNIQUE (execucao_id, no_id)
);

CREATE TABLE agendamentos (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_id uuid NOT NULL REFERENCES pipelines(id),
  cron        text NOT NULL,
  timezone    text NOT NULL,
  habilitado  boolean NOT NULL DEFAULT true,
  criado_por  uuid REFERENCES usuarios(id)
);
```

## 5. Cubes, views and pages

Cubes, views and pages are versioned JSON documents (`cubos`/`cubo_versoes`, `views`/`view_versoes`,
`paginas`/`pagina_versoes`) with the draft vs published lifecycle of section 3. Measures live inside
the cube JSON. `pagina_permissoes` lists which groups see a page in the catalogue.

## 6. Stamped reports

```sql
CREATE TABLE relatorios (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pagina_versao_id  uuid NOT NULL REFERENCES pagina_versoes(id),
  execucao_id       uuid NOT NULL REFERENCES execucoes(id),  -- the run that produced the data
  gerado_por        uuid REFERENCES usuarios(id),
  filtros_aplicados jsonb NOT NULL,
  formato           text NOT NULL,        -- 'xlsx'
  carimbo           jsonb NOT NULL,       -- time, version, filters, user (immutable mirror)
  arquivo_ref       text NOT NULL,
  criado_em         timestamptz NOT NULL DEFAULT now()
);
```

## 7. Report layouts and exports (migrations 0036, 0042)

A layout is versioned JSON (`relatorio_layouts` + `relatorio_layout_versoes`). An export is the
ARTIFACT of the tuple `(run, node, layout, version, format)`; `relatorio_exportacoes.chave` is the
sha256 of that tuple and a partial unique index (`WHERE estado <> 'falha'`) makes generation
idempotent while letting a failed attempt be retried. Exports have no `DELETE` grant (provenance).
A *grouped* layout (`tipo: agrupado`) combines several single-source layouts as workbook sheets;
its parts are recorded in `relatorio_exportacao_partes`.

## 8. Audit trail (APPEND-ONLY)

```sql
CREATE TABLE auditoria (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ocorrido_em   timestamptz NOT NULL DEFAULT now(),
  ator_id       uuid REFERENCES usuarios(id),
  ator_tipo     text NOT NULL DEFAULT 'usuario',   -- usuario | sistema
  acao          text NOT NULL,          -- criar | editar | publicar | excluir | login | ...
  recurso_tipo  text NOT NULL,
  recurso_id    uuid,
  antes         jsonb,
  depois        jsonb,
  request_id    text,
  ip            inet
);
-- The runtime role has only INSERT and SELECT on this table.
```

## Relationships

```
usuarios --< usuarios_grupos >-- grupos
usuarios --< permissoes (subject) ; permissoes >-- (resource: pipeline/cubo/view/pagina/conexao)
pipelines --< pipeline_versoes --< execucoes --< execucao_nos   (execucoes.cadeia_id groups a chain)
pipelines --< cubos --< cubo_versoes ; cubos --< views --< view_versoes
paginas --< pagina_versoes --< relatorios >-- execucoes
relatorio_layouts --< relatorio_layout_versoes --< relatorio_exportacoes >-- execucoes
ingestao_fontes --< ingestao_execucoes --< ingestao_fatias ; ingestao_fontes --< _lotes / _watermarks / _quarentena
conexoes --> cofre_credenciais (credencial_ref)
(all writes) --> auditoria
```
