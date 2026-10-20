"""Integration fixtures. They require the docker-compose Postgres to be up.

Required environment variables (see .env.example):
  CFLOW_OWNER_DATABASE_URL  - connection as cflow_owner (migrations, setup, cleanup)
  CFLOW_DATABASE_URL        - connection as cflow_app (runtime; what the application uses)
  CFLOW_APP_PASSWORD        - password of the cflow_app role (used by migration 0002)
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid as uuid_mod
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ._plataforma import RAIZ_PLATAFORMA as RAIZ

# OPTIONAL harness suffix (`CFLOW_SUFIXO_TESTE`). Empty = `cflow_test`, exactly as it has always
# been, so `make test` does not change database.
#
# The test database name is GLOBAL to the machine, not to the checkout: two pytest sessions at the
# same time (two worktrees, a `make test` while another runs) truncate the SAME `cflow_test`, and
# one session's TRUNCATE wipes the other's login session mid-test, so the victim fails with
# `nao_autenticado` having done nothing wrong, in different tests on every run. The `flock` in
# `_sessao_exclusiva` makes the second one WAIT; the suffix is for those who prefer to run both IN
# PARALLEL on separate databases: `CFLOW_SUFIXO_TESTE=_x pytest ...` (creates and migrates
# `cflow_test_x`). It is the only way out when the other checkout runs an older conftest that does
# not know the lock.
SUFIXO_TESTE = os.environ.get("CFLOW_SUFIXO_TESTE", "")
DB_TESTE = (
    f"cflow_test{SUFIXO_TESTE}"  # isolated database: the dev database (cflow) is never touched
)

# The Temporal task queue is UNIQUE PER pytest SESSION, always. A fixed name
# (`cflow-execucao-test`) is a GLOBAL address on the Temporal server: with two sessions up, one's
# worker picks up the other's workflow and runs it with ITS OWN CODE. The test then reports exactly
# the defect it exists to catch, over code that is correct: the worst possible false positive.
# A queue is just a string: making it unique costs nothing and closes the door.
_SESSAO = uuid_mod.uuid4().hex[:8]
FILA_EXECUCAO_TESTE = f"cflow-execucao-test{SUFIXO_TESTE}-{_SESSAO}"
FILA_INGESTAO_TESTE = f"cflow-ingestao-test{SUFIXO_TESTE}-{_SESSAO}"
FILA_RELATORIOS_TESTE = f"cflow-relatorios-test{SUFIXO_TESTE}-{_SESSAO}"


def _owner_url() -> str:
    url = os.environ.get("CFLOW_OWNER_DATABASE_URL")
    if not url:
        pytest.skip("CFLOW_OWNER_DATABASE_URL is not set - start the compose Postgres.")
    return url


def _trocar_db(url: str, db: str) -> str:
    """Replace the database name in the DSN (last path segment)."""
    base, _, _ = url.rpartition("/")
    return f"{base}/{db}"


async def _criar_db_teste(owner_url_original: str) -> None:
    import asyncpg
    from sqlalchemy import make_url

    u = make_url(owner_url_original)
    conn = await asyncpg.connect(
        host=u.host, port=u.port, user=u.username, password=u.password, database="postgres"
    )
    try:
        existe = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB_TESTE)
        if not existe:
            await conn.execute(f'CREATE DATABASE "{DB_TESTE}"')
    finally:
        await conn.close()


def aplicar_migrations() -> None:
    """Run `alembic upgrade head` programmatically (as cflow_owner)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(RAIZ / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "migrations"))
    command.upgrade(cfg, "head")


def pytest_collection_modifyitems(config, items):
    """Every test in this folder is an INTEGRATION test, with or without `pytestmark` in the file.

    CI splits the two jobs by marker (`-m integration` / `-m "not integration"`), and files that
    did not declare it ran nowhere: in the integration job they were DISCARDED by the filter, and
    in the unit job they were SKIPPED for lack of a database, while CI stayed green. Marking by
    folder makes the rule the file's location, not the memory of whoever wrote it."""
    pasta = Path(__file__).parent.resolve()
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(pasta):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def _sessao_exclusiva():
    """ONE pytest session per test database, on the MACHINE, not per checkout.

    `cflow_test` and the `cflow-*-test` queues are machine-global names. Two sessions at once (two
    worktrees, a `make test` while another runs) use the SAME database: one's `TRUNCATE` wipes,
    mid-test, the session of whoever was authenticated in the other, and the victim fails with
    `nao_autenticado`/`nao_autorizado` or with `deadlock detected` in setup, in a test that did
    nothing wrong. Worse: the symptom lands on DIFFERENT tests on every run and looks exactly like
    a test that leaks work, so the investigation chases the wrong target.

    The lock is a file in the temp dir with `flock`: the second session WAITS for the first to
    finish instead of running over it. The OS releases the lock when the process dies, so `Ctrl-C`
    or a crash leaves nobody stuck. To run both IN PARALLEL use different databases
    (`CFLOW_SUFIXO_TESTE`), which yields different lock files."""
    try:
        import fcntl
    except ImportError:  # platform without flock: proceed without the lock
        yield
        return
    caminho = Path(tempfile.gettempdir()) / f"{DB_TESTE}.lock"
    fh = caminho.open("w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(
                f"\n[harness] another pytest session is using database {DB_TESTE}. "
                "Waiting for its turn (running on top would corrupt BOTH). "
                "To run in parallel: CFLOW_SUFIXO_TESTE=<something> pytest ...",
                flush=True,
            )
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@pytest.fixture(scope="session", autouse=True)
def _migrado(_sessao_exclusiva) -> None:
    """Create the isolated test database (cflow_test), redirect ALL configuration to it and migrate.

    This way `make test` never touches the dev database (cflow).
    """
    import asyncio

    owner = os.environ.get("CFLOW_OWNER_DATABASE_URL")
    app = os.environ.get("CFLOW_DATABASE_URL")
    if not owner or not app:
        pytest.skip("CFLOW_OWNER_DATABASE_URL/CFLOW_DATABASE_URL are not set - start Postgres.")

    asyncio.run(_criar_db_teste(owner))
    # redirect everything (test engines + the app under test) to the isolated database
    os.environ["CFLOW_OWNER_DATABASE_URL"] = _trocar_db(owner, DB_TESTE)
    os.environ["CFLOW_DATABASE_URL"] = _trocar_db(app, DB_TESTE)
    # Temporal is part of the stack (execution = workflow). On the host it is at localhost:7233
    # (the .env `temporal:7233` is a container DNS name). The queue is ISOLATED from the dev worker
    # (cflow-execucao), so the in-process test worker is the only one that picks up the tests'
    # workflows, against cflow_test.
    os.environ["CFLOW_TEMPORAL_HOST"] = os.environ.get(
        "CFLOW_TEMPORAL_HOST_TESTE", "localhost:7233"
    )
    os.environ["CFLOW_TEMPORAL_TASK_QUEUE"] = FILA_EXECUCAO_TESTE
    os.environ["CFLOW_INGESTAO_TASK_QUEUE"] = FILA_INGESTAO_TESTE
    os.environ["CFLOW_RELATORIOS_TASK_QUEUE"] = FILA_RELATORIOS_TESTE
    # Lake in a temp dir: ingestion writes landing/bronze to disk on the local backend; without
    # this, `make test` would leave junk in the repository.
    os.environ.setdefault("CFLOW_LAKE_DIR", os.path.join(tempfile.gettempdir(), "cflow_lake_test"))
    # And the harness never talks to a bucket: a lake connection that is not local is REFUSED
    # (`lake_somente_local`, see `cflow_core.ingestao.armazenamento`). Assignment, not
    # `setdefault`: a forgotten `false` in the shell must not enable the object-storage path in
    # the middle of the suite. A test that exercises the object-storage backend against a simulated
    # transport turns the guard off itself, by monkeypatch, and says so in the test.
    os.environ["CFLOW_LAKE_SOMENTE_LOCAL"] = "true"
    from cflow_core.config import get_settings

    get_settings.cache_clear()
    aplicar_migrations()


@pytest_asyncio.fixture
async def owner_engine():
    engine = create_async_engine(_owner_url(), future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine():
    url = os.environ.get("CFLOW_DATABASE_URL")
    if not url:
        pytest.skip("CFLOW_DATABASE_URL is not set.")
    engine = create_async_engine(url, future=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _engine_por_teste():
    """The global engine is cached PER loop; pytest-asyncio uses one loop per test. Dispose of THIS
    loop's engine before and after, without touching the in-process Temporal worker's (another
    loop)."""
    from cflow_core.db import dispor_engine_do_loop

    await dispor_engine_do_loop()
    yield
    await dispor_engine_do_loop()


@pytest_asyncio.fixture(autouse=True)
async def _limpar_estado(owner_engine):
    """Clean the trail + auth state between tests (through the owner role: cflow_app cannot delete
    audit rows).

    CASCADE covers the FKs to usuarios; RESTART IDENTITY resets the counters.

    BEFORE truncating, wait for whatever the previous test left in flight in Temporal to close
    (`_drenar_trabalho_em_voo`): the TRUNCATE and an activity still writing fight over the same
    tables, producing `deadlock detected` in setup or an activity that returns with
    `ForeignKeyViolationError` and deletes the next test's session from under it.
    """
    await _drenar_trabalho_em_voo(owner_engine)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE auditoria, sessoes, tokens_api, tokens_recuperacao, tentativas_login, "
                "permissoes, usuarios_grupos, grupos, conexoes, cofre_credenciais, "
                "ingestao_quarentena, ingestao_watermarks, ingestao_lotes, ingestao_fatias, "
                "ingestao_execucoes, ingestao_fontes, "
                "biblioteca_funcoes_versoes, biblioteca_funcoes, "
                "relatorio_exportacao_partes, relatorio_exportacoes, "
                "relatorio_layout_versoes, relatorio_layouts, "
                "relatorios, pagina_versoes, paginas, cubo_versoes, cubos, view_versoes, views, "
                "agendamentos, execucao_nos, execucoes, pipeline_versoes, pipelines, usuarios "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def cliente():
    """ASGI HTTP client for the API (no network)."""
    from httpx import ASGITransport, AsyncClient

    from cflow_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://teste") as c:
        yield c


# ---- Temporal: in-process worker for the tests (execution runs through the workflow, not inline) ----

_WORKER: dict = {"ok": False, "motivo": "not started", "client": None, "loop": None}


# ---- Test boundary: nothing in flight crosses the next test's TRUNCATE ----------------------
#
# The Temporal worker runs in a separate THREAD and the activity it executes writes to the SAME
# database. If a workflow is still running when the NEXT test's `_limpar_estado` truncates, the two
# fight over the same tables: either `deadlock detected` in setup (the TRUNCATE wants an
# AccessExclusiveLock on `execucao_nos` while the activity holds a RowExclusiveLock on
# `execucoes`), or the TRUNCATE wins and the activity returns with `ForeignKeyViolationError`, and
# the next test, whose session was deleted from under it, fails with `nao_autenticado`. The
# symptom never shows up in the guilty test.
#
# This is a GUARD, not the fix for a measured leak: probing the boundary showed no workflow still
# `RUNNING`, because `ExecutarPipeline` only calls `finalizar_act` AFTER the last attempt, so "the
# row became `falha`" already implies "retries are over". What really broke the suite was
# something else: the database and queue shared between TWO pytest sessions (see
# `_sessao_exclusiva` and the per-session queue).
#
# It stays because it costs ~0 (it never actually waited) and because it turns an unreadable
# `deadlock detected` into a message naming the workflow, the day a new path (effect node,
# `ExecutarDAG` aborting a sibling) hands control back before the workflow closes.
#
# The wait is on STATE (the workflow closed), never on time: a fixed `sleep` would just trade one
# intermittent green for another, slower one.


def _no_loop_do_worker(fabricar_coro, *, timeout: float):
    """Run a coroutine ON THE WORKER'S LOOP (its own thread) and block until the result.

    The Temporal client belongs to the loop where it was created; using it from the test loop
    misuses the SDK. Blocking the test thread is harmless: the other loop does the work."""
    import asyncio as aio

    loop = _WORKER.get("loop")
    cliente_tmp = _WORKER.get("client")
    if loop is None or cliente_tmp is None:
        return None
    return aio.run_coroutine_threadsafe(fabricar_coro(cliente_tmp), loop).result(
        timeout=timeout + 30
    )


def _encerrar_workflows_da_sessao() -> None:
    """At the END of the session, terminate whatever stayed `Running` ON THIS SESSION'S QUEUE.

    Since the queue is unique per session, nobody else adopts those workflows: without this they
    would stay `Running` in Temporal forever, with the server rescheduling workflow tasks for a
    worker that no longer exists. Only the session's own queue is touched: terminating by prefix
    would kill the run of a colleague running alongside."""
    from cflow_core.config import get_settings

    cfg = get_settings()
    filas = {cfg.temporal_task_queue, cfg.ingestao_task_queue}

    async def _encerrar(cliente_tmp):
        for fila in filas:
            try:
                async for wf in cliente_tmp.list_workflows(
                    f"TaskQueue = '{fila}' AND ExecutionStatus = 'Running'"
                ):
                    try:
                        await cliente_tmp.get_workflow_handle(wf.id, run_id=wf.run_id).terminate(
                            "end of test session"
                        )
                    # noqa S112: best-effort cleanup in the suite teardown; logging here would
                    # only fill the pytest output with noise about a workflow that already died.
                    except Exception:  # noqa: BLE001,S112 - already closed / no longer exists
                        continue
            except Exception:  # noqa: BLE001,S112 - visibility being down must not fail the suite
                continue

    try:
        _no_loop_do_worker(_encerrar, timeout=30)
    except Exception:  # noqa: BLE001
        pass


async def _aguardar_workflows(ids: list[str], *, timeout: float = 90.0) -> None:
    """Wait for each workflow in the list to CLOSE (any outcome). A nonexistent id is ignored.

    Queries the workflow by id (`describe`), not visibility (`list_workflows`): visibility is
    eventually consistent and a just-closed workflow still shows as `Running` there, so a gate
    built on it sometimes waits needlessly and sometimes lets things through."""
    if not ids or not _WORKER.get("ok"):
        return

    async def _esperar(cliente_tmp):
        import asyncio as aio

        from temporalio.client import WorkflowExecutionStatus

        loop = aio.get_running_loop()
        fim = loop.time() + timeout
        for wid in ids:
            handle = cliente_tmp.get_workflow_handle(wid)
            while True:
                try:
                    desc = await handle.describe()
                except Exception:  # noqa: BLE001 - workflow that never existed: nothing to wait for
                    break
                if desc.status != WorkflowExecutionStatus.RUNNING:
                    break
                if loop.time() > fim:
                    raise AssertionError(
                        f"workflow {wid} is still running after {timeout}s: the test "
                        "would be leaking work into the next test's TRUNCATE."
                    )
                await aio.sleep(0.05)

    _no_loop_do_worker(_esperar, timeout=timeout)


def _ids_de_workflow(execucoes: list[str]) -> list[str]:
    """The workflow ids the platform derives from an `execucao_id`: `exec-{id}` for
    `ExecutarPipeline` (including the chain's child) and `dag-{id}` for `ExecutarDAG` (the id is
    the ROOT's). Nonexistent ones are ignored while waiting."""
    return [f"dag-{e}" for e in execucoes] + [f"exec-{e}" for e in execucoes]


async def _drenar_trabalho_em_voo(owner_engine) -> None:
    """Gate of `_limpar_estado`: before truncating, wait for every workflow of the executions in
    the database to close. An execution ABANDONED on purpose (the fan-out test measures the
    pre-created ones with a starter that only records) has no workflow behind it: `describe`
    returns nonexistent and the wait is instantaneous, without stalling the suite."""
    if not _WORKER.get("ok"):
        return
    async with owner_engine.connect() as conn:
        ids = [str(r[0]) for r in (await conn.execute(text("SELECT id FROM execucoes"))).all()]
    await _aguardar_workflows(_ids_de_workflow(ids))


@pytest.fixture(scope="session", autouse=True)
def _worker_temporal(_migrado):
    """Start an IN-PROCESS Temporal worker (own thread + event loop) bound to cflow_test, on the
    session-isolated queue. Temporal is part of the stack, so execution tests go through the real
    workflow (each node = an activity). If Temporal is down it marks itself unavailable and only
    the tests that wait for an execution are skipped (see `aguardar_execucao`)."""
    import importlib.util

    from cflow_core.config import get_settings

    cfg = get_settings()
    # Without the worker package the thread below would die on import, the harness would mark
    # "Temporal unavailable" and the execution tests would be SKIPPED: an empty green. Refusing
    # here names what is missing.
    if cfg.usar_temporal and importlib.util.find_spec("cflow_worker") is None:
        raise RuntimeError(
            "cflow_worker not found: point CFLOW_WORKER_DIR at the worker checkout (or place it "
            "in `worker/` at the repository root). To run without a worker, "
            "CFLOW_USAR_TEMPORAL=false."
        )
    estado: dict = {}
    pronto = threading.Event()

    def _rodar() -> None:
        import asyncio as aio

        async def _main() -> None:
            from temporalio.client import Client
            from temporalio.worker import Worker

            from cflow_worker.activities import executar_no_act, finalizar_act
            from cflow_worker.ingestao_activities import (
                concluir_ingestao_act,
                falhar_ingestao_act,
                ingerir_fatia_act,
            )
            from cflow_worker.ingestao_workflow import IngerirFonte
            from cflow_worker.relatorios_activities import gerar_relatorio_act
            from cflow_worker.relatorios_workflow import GerarRelatorio
            from cflow_worker.workflow import ExecutarDAG, ExecutarPipeline

            try:
                client = await Client.connect(cfg.temporal_host)
            except Exception as e:  # noqa: BLE001
                estado["erro"] = repr(e)
                pronto.set()
                return
            # The client is made available to the harness (`_drenar_trabalho_em_voo`), always USED
            # ON THIS loop through `run_coroutine_threadsafe`: the temporalio client is bound to
            # the loop where it was created, and calling it from the test loop is a misuse.
            estado["client"] = client
            parar = aio.Event()
            estado["loop"] = aio.get_running_loop()
            estado["parar"] = parar
            # Two queues, as in production: ingestion has its own so a backfill does not compete
            # for slots with pipeline execution.
            async with (
                Worker(
                    client,
                    task_queue=cfg.temporal_task_queue,
                    workflows=[ExecutarPipeline, ExecutarDAG],
                    activities=[executar_no_act, finalizar_act],
                ),
                Worker(
                    client,
                    task_queue=cfg.ingestao_task_queue,
                    workflows=[IngerirFonte],
                    activities=[ingerir_fatia_act, concluir_ingestao_act, falhar_ingestao_act],
                ),
                # Third queue, for reports: one file = one activity, 4 in parallel.
                Worker(
                    client,
                    task_queue=cfg.relatorios_task_queue,
                    workflows=[GerarRelatorio],
                    activities=[gerar_relatorio_act],
                    max_concurrent_activities=4,
                ),
            ):
                pronto.set()
                await parar.wait()

        try:
            aio.run(_main())
        except Exception as e:  # noqa: BLE001
            estado.setdefault("erro", repr(e))
            pronto.set()

    t = threading.Thread(target=_rodar, name="temporal-worker-teste", daemon=True)
    t.start()
    pronto.wait(timeout=20)
    if estado.get("erro") or "parar" not in estado:
        _WORKER["ok"] = False
        _WORKER["motivo"] = estado.get("erro", "worker did not start (timeout)")
    else:
        _WORKER["ok"] = True
        _WORKER["client"] = estado.get("client")
        _WORKER["loop"] = estado.get("loop")
    yield
    if _WORKER["ok"]:
        _encerrar_workflows_da_sessao()
    loop = estado.get("loop")
    parar = estado.get("parar")
    if loop is not None and parar is not None:
        import asyncio as aio

        async def _fim() -> None:
            from cflow_core.db import dispor_engine_do_loop

            await dispor_engine_do_loop()
            parar.set()

        try:
            aio.run_coroutine_threadsafe(_fim(), loop).result(timeout=10)
        except Exception:  # noqa: BLE001
            pass
    t.join(timeout=10)


_AMBIENTE_PARAMETRO: dict = {
    "nome": "ambiente",
    "rotulo": "Environment",
    "tipo": "opcoes",
    "opcoes": ["prd", "dev"],
    "obrigatorio": True,
    "descricao": "Where the run writes and reads the gold layer: prd or dev.",
}


def _usa_gold(no: dict) -> bool:
    cfg = no.get("config") or {}
    if no.get("tipo") == "saida":
        return (
            str(cfg.get("destino") or "arquivo").lower() == "lake"
            and (cfg.get("camada") or "silver") == "gold"
        )
    if no.get("tipo") == "fonte":
        return cfg.get("origem") == "publicado" and (cfg.get("camada") or "gold") == "gold"
    return False


def contrato_ambiente_gold(definicao: dict, *, declarar: bool = False) -> dict:
    """Declare the mandatory `ambiente` parameter on synthetic definitions that read or write the
    gold layer (or chain pipelines that do, with `declarar=True`)."""
    import copy

    nova = copy.deepcopy(definicao)
    precisa = declarar or any(_usa_gold(n) for n in nova.get("nos") or [])
    tem = any(p.get("nome") == "ambiente" for p in nova.get("parametros") or [])
    if precisa and not tem:
        nova["parametros"] = [*(nova.get("parametros") or []), dict(_AMBIENTE_PARAMETRO)]
    return nova


def parametros_ambiente_gold(definicao: dict, parametros: dict | None = None) -> dict:
    """Include ``dev`` only when the definition declares the mandatory gold context."""
    valores = dict(parametros or {})
    if any(p.get("nome") == "ambiente" for p in definicao.get("parametros") or []):
        valores.setdefault("ambiente", "dev")
    return valores


async def aguardar_execucao(cliente, exec_id: str, headers: dict, *, timeout: float = 60.0) -> dict:
    """Wait for the Temporal workflow to finish and return the detail (GET /execucoes/{id}, with
    `nos`). Skips the test if the test worker did not start; does NOT fall back to inline
    (Temporal is the stack)."""
    import asyncio as aio

    if not _WORKER["ok"]:
        pytest.skip(
            f"Test Temporal worker unavailable ({_WORKER['motivo']}) - "
            "start it with `docker compose --profile temporal up -d`."
        )
    loop = aio.get_running_loop()
    fim = loop.time() + timeout
    ultimo: dict = {}
    while loop.time() < fim:
        ultimo = (await cliente.get(f"/execucoes/{exec_id}", headers=headers)).json()
        if ultimo.get("status") in ("concluida", "falha", "erro"):
            # The decided row is almost always the end, but the WORKFLOW is what closes the matter:
            # waiting for it keeps the test from returning control while an activity is still
            # writing to the database the next TRUNCATE will clean. In practice it is already
            # closed every time (see the note above), so this costs no wait.
            await _aguardar_workflows(_ids_de_workflow([str(exec_id)]))
            return ultimo
        await aio.sleep(0.25)
    raise AssertionError(
        f"execution {exec_id} did not finish in {timeout}s (status={ultimo.get('status')})"
    )
