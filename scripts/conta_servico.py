"""Create (or verify) the SERVICE account that triggers pipelines, without HTTP and without a
secret in plain sight.

    # inside the api container. The RECOMMENDED path is the `servico` profile: it signs in only
    # through an API token, triggers and follows what it may execute, and authors nothing.
    uv run python scripts/conta_servico.py --email svc.etl@example.com \\
        --name 'Service account - ETL' --profile servico \\
        --token-file /run/secrets/cflow-token --token-name 'ci-adapter' \\
        --execute 'load_orders' --execute 'export_invoices' --dry-run
    # ...and without --dry-run to write, saying who runs it (an active admin; goes to the trail):
    #   ... --actor admin@example.com
    # The previous path (specialist profile, with a password) still works: `--password-file
    # <path>` or `--password-stdin` (reads the password from standard input, no echo on a tty).

It exists because whatever triggers pipelines from outside (a scheduler, a CI adapter) needs an
identity of its own, and the UI path delivers the first password by e-mail, which is the wrong
channel for an account no human opens. Here the writes use the SAME core services
(`usuarios.criar`, `tokens_api.emitir`, `recuperacao.confirmar`, `permissoes.conceder`), with the
`--actor` admin as the actor, so everything is audited exactly like the API path.

**What the script grants, and why.** EXECUTE permission on the requested roots: that is what
triggering requires (`POST /pipelines/{id}/executar-encadeado` = a profile that executes +
`permissoes.exigir(..., 'executar')`).

**The `servico` profile: least privilege.** It triggers and follows ONLY the pipelines on which it
has `executar` (including an execution another actor started), lists only those, and creates,
edits and publishes nothing. It has no password: it signs in with an API token. `--token-file`
issues the token and writes it to a NEW file, created exclusively with mode 0600: the token never
goes to stdout, the log or the trail (which keeps name, prefix and expiry). A file that already
exists is refused before any write: running again with the same path does not issue another token
over the one in use.

**The `especialista` profile (the default, and what existed before).** It triggers what it has
`executar` on, but it is NOT "execute only": a specialist also creates its own pipelines
(`POST /pipelines` only requires Editor, and the creator administers what it creates), writes
nodes on any existing connection and runs them. Its password is worth a specialist's and must be
kept that way. This is pinned by a test
(`test_limite_conhecido_a_conta_cria_pipeline_proprio`) so nobody reads this path as delivering
more than it does; the way out is `--profile servico`.

**The password never goes through an argument**: arguments leak into `ps` and shell history. A
hidden `--password` exists only to REFUSE without echoing the value; without it, argparse would
answer "unrecognized arguments: --password <value>", repeating the password in the terminal.

**The first-password token does not leave the process.** The core flow generates a token and
delivers it through the notification channel; without SMTP the channel is the console, which
PRINTS the token. Here the token is generated and consumed in the same transaction, and the
channel is never called. (A `servico` account has no first password.)

**Setting a password closes the other doors.** Every time the script sets the password (new
account, account that existed without one, `--reset-password`), in the SAME transaction: it burns
the account's pending recovery and first-password tokens (otherwise the e-mailed link would reset
over it) and revokes ALL of its sessions (resetting is the rotation after a leak; whoever kept the
Bearer for the old password gets 401 on the next request). The counts go to the trail; no secret.

**Idempotent**: an existing account does not have its password changed (only with
`--reset-password`), and an identical permission is not duplicated, so running again does not
write into the append-only trail a record of a change that did not happen. The exception is an
account created through the UI whose first password was never set (`senha_hash` null): "kept"
would mean the script succeeded while login answers 401, so it receives the given password
(without one, it is a named refusal). An existing account with ANOTHER profile, or a suspended one,
is refused: changing a profile or re-enabling access is a decision for whoever administers, through
the UI, not a side effect of a script. The token is the declared exception: `--token-file` issues
a new token on every run, which is why the file has to be new.

**All or nothing.** A missing `--actor` (or one that is not an active admin), a pipeline that is
missing, ambiguous (the name is NOT unique in the database) or flagged `sistema`, a password for a
`servico` account, a token for a non-`servico` account and an existing token file are all refused
BEFORE any write. `sistema` because the flag overrides the fine-grained permission
(`permissoes.exigir`): the account would receive the permission and still get 403 when triggering.
A failure midway undoes the account, the token AND the file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import sys
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
for _pacote in ("core/src", "api/src", "scripts"):
    _caminho = str(RAIZ / _pacote)
    if _caminho not in sys.path:
        sys.path.insert(0, _caminho)

from sqlalchemy import text  # noqa: E402

from cflow_core.audit import Acao, registrar  # noqa: E402
from cflow_core.auth import recuperacao, tokens_api  # noqa: E402
from cflow_core.auth.senhas import validar_politica  # noqa: E402
from cflow_core.authz import permissoes  # noqa: E402
from cflow_core.authz.perfis import Nivel, Perfil  # noqa: E402
from cflow_core.db import transacao  # noqa: E402
from cflow_core.errors import DetalheErro, ErroAplicacao  # noqa: E402
from cflow_core.timezone import para_exibicao  # noqa: E402
from cflow_core.usuarios import service as usuarios  # noqa: E402

ORIGEM = "scripts/conta_servico.py"
NIVEL = Nivel.EXECUTAR
ROTULO_PADRAO = "ci-adapter"
SENHA_NAO_SE_APLICA = "password   not applicable (service account signs in with an API token)"


async def _resolver_ator(conn, email: str | None) -> uuid.UUID:
    """The id of the active admin with this e-mail (case-insensitive).

    The audit trail is append-only and permanent: picking "the first active admin by `criado_em`"
    would record that a specific person (usually the bootstrap account) did something they never
    saw, with no way to correct it later. So `--actor <email>` is mandatory when writing, and it
    must be an ACTIVE admin; missing, unknown or without the profile is a named refusal.
    """
    if not email or not email.strip():
        raise ValueError(
            "provide --actor <e-mail of an active admin>: the trail is permanent and must say "
            "WHO ran the script."
        )
    linha = (
        await conn.execute(
            text("SELECT id, perfil, ativo FROM usuarios WHERE lower(email) = lower(:e)"),
            {"e": email.strip()},
        )
    ).first()
    if linha is None:
        raise ValueError(f"--actor '{email}' does not exist in this database.")
    if linha.perfil != "admin" or not linha.ativo:
        raise ValueError(f"--actor '{email}' is not an active admin.")
    return linha.id


async def _ator(conn, email: str | None) -> uuid.UUID:
    """The `--actor` admin, wrapped in the script's error envelope."""
    try:
        return await _resolver_ator(conn, email)
    except ValueError as exc:
        raise ErroAplicacao(
            codigo="ator_invalido", mensagem=f"Nothing was written: {exc}", status=400
        ) from exc


async def _resolver_pipelines(conn, nomes: list[str]) -> list[tuple[str, uuid.UUID]]:
    """Name -> id, refusing missing/ambiguous/system pipelines. Collects ALL problems into a single
    refusal so the operator fixes the list in one go instead of finding one name per run."""
    resolvidos: list[tuple[str, uuid.UUID]] = []
    problemas: list[tuple[str, str, str]] = []
    for nome in dict.fromkeys(nomes):  # a repeated name on the command line is the same target
        linhas = (
            await conn.execute(
                text("SELECT id, sistema FROM pipelines WHERE nome = :n ORDER BY criado_em"),
                {"n": nome},
            )
        ).all()
        if not linhas:
            problemas.append(
                ("pipeline_ausente", nome, f"pipeline '{nome}' does not exist in this database")
            )
        elif len(linhas) > 1:
            problemas.append(
                (
                    "pipeline_ambiguo",
                    nome,
                    f"there are {len(linhas)} pipelines named '{nome}'; granting by name "
                    "would pick one of them blindly",
                )
            )
        elif linhas[0].sistema:
            problemas.append(
                (
                    "pipeline_sistema",
                    nome,
                    f"pipeline '{nome}' is a 'system' pipeline: only admins can access it, and "
                    "the granted permission would be denied with a 403 anyway",
                )
            )
        else:
            resolvidos.append((nome, linhas[0].id))
    if problemas:
        codigos = {p[0] for p in problemas}
        raise ErroAplicacao(
            codigo=codigos.pop() if len(codigos) == 1 else "pipelines_invalidos",
            mensagem="Nothing was written: " + "; ".join(p[2] for p in problemas) + ".",
            status=400,
            detalhes=[DetalheErro(campo=f"--execute {p[1]}", mensagem=p[2]) for p in problemas],
        )
    return resolvidos


def _conferir_pedido_de_token(servico: bool, caminho: Path, nome: str, dias: int) -> None:
    """Everything the token request needs, checked BEFORE any write. The guarantee that the file is
    new is `O_EXCL` at write time; checking here makes the refusal come before the account is
    created."""
    if not servico:
        raise ErroAplicacao(
            codigo="token_so_para_servico",
            mensagem="Nothing was written: an API token is only for a service account "
            "(--profile servico); people sign in with a password and MFA.",
            status=400,
        )
    if not (nome or "").strip():
        raise ErroAplicacao(
            codigo="nome_invalido",
            mensagem="Nothing was written: give the token a name (--token-name), i.e. where it "
            "will live.",
            status=400,
        )
    if isinstance(dias, bool) or not isinstance(dias, int) or not 1 <= dias <= tokens_api.DIAS_MAX:
        raise ErroAplicacao(
            codigo="validade_invalida",
            mensagem=f"Nothing was written: the token lifetime ranges from 1 to "
            f"{tokens_api.DIAS_MAX} days (--token-days).",
            status=400,
        )
    if caminho.exists() or caminho.is_symlink():
        raise ErroAplicacao(
            codigo="token_arquivo_existe",
            mensagem=f"Nothing was written: {caminho} already exists. The token goes to a NEW "
            "file; running again with the same path would issue another token over the one in "
            "use.",
            status=400,
        )
    if not caminho.parent.is_dir():
        raise ErroAplicacao(
            codigo="token_arquivo_invalido",
            mensagem=f"Nothing was written: the folder {caminho.parent} does not exist.",
            status=400,
        )


def _gravar_token(caminho: Path, token: str, escritos: list[Path]) -> None:
    """Create the file EXCLUSIVELY (`O_EXCL`: neither over an existing file nor following a link)
    already with mode 0600 and write the token. The mode is set at `open`; creating and then
    running `chmod` would leave a window in which the file is readable by others. `escritos`
    receives the path as soon as it exists, so the "all or nothing" cleanup also reaches a write
    that failed halfway."""
    try:
        fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ErroAplicacao(
            codigo="token_arquivo_existe",
            mensagem=f"Nothing was written: {caminho} came into existence during the run.",
            status=400,
        ) from exc
    except OSError as exc:  # only the OS reason
        raise ErroAplicacao(
            codigo="token_arquivo_invalido",
            mensagem=f"Nothing was written: could not create {caminho} ({exc.strerror}).",
            status=400,
        ) from exc
    escritos.append(caminho)
    with os.fdopen(fd, "w", encoding="utf-8") as arq:
        arq.write(token + "\n")


async def _definir_senha(
    conn, usuario_id: uuid.UUID, senha: str, ator: uuid.UUID, *, token: str | None, motivo: str
) -> tuple[int, int]:
    """Set the password and close the account's other doors; returns (revoked sessions, invalidated
    pending tokens).

    This is the core's first-password flow (single-use token -> `confirmar`, which validates the
    policy before consuming), minus the delivery channel: the token is consumed IN THE SAME
    transaction in which it is born. Then, still in the same transaction:
    - pending recovery/first-password tokens are burned: the 72 h link the UI e-mailed would
      otherwise reset the password over this one, in the hands of whoever owns the mailbox;
    - ALL of the account's sessions are revoked: without this, resetting after a leak left the
      Bearer obtained with the old password valid until it expired.

    The trail is written by hand rather than through `registrar_senha_trocada`: that helper pins
    the user THEMSELVES as the actor, which is true on the recovery screen and false here, where
    the password was set by the admin who ran the script."""
    if token is None:
        # Short window (the recovery one): the token does not outlive this function anyway.
        token = await recuperacao.iniciar_definicao_senha(conn, usuario_id, primeira=False)
    await recuperacao.confirmar(conn, token, senha)
    tokens_invalidados = (
        await conn.execute(
            text(
                "UPDATE tokens_recuperacao SET usado_em = now() "
                "WHERE usuario_id = :u AND usado_em IS NULL"
            ),
            {"u": usuario_id},
        )
    ).rowcount
    sessoes_revogadas = (
        await conn.execute(
            text(
                "UPDATE sessoes SET revogada_em = now() "
                "WHERE usuario_id = :u AND revogada_em IS NULL"
            ),
            {"u": usuario_id},
        )
    ).rowcount
    await registrar(
        conn,
        acao=Acao.SENHA_TROCADA,
        recurso_tipo="usuario",
        recurso_id=usuario_id,
        ator_id=ator,
        antes=None,
        # NO password, token or hash: only what happened and how many doors were closed.
        depois={
            "via": ORIGEM,
            "motivo": motivo,
            "sessoes_revogadas": sessoes_revogadas,
            "tokens_invalidados": tokens_invalidados,
        },
    )
    return sessoes_revogadas, tokens_invalidados


async def _permissao_existe(conn, usuario_id: uuid.UUID, pipeline_id: uuid.UUID) -> bool:
    """The SAME equality `permissoes.conceder` uses to be idempotent (identical row), so the dry
    run predicts exactly what the real run would do."""
    return (
        await conn.execute(
            text(
                "SELECT 1 FROM permissoes WHERE sujeito_tipo = 'usuario' AND sujeito_id = :u "
                "AND recurso_tipo = 'pipeline' AND recurso_id = :p AND nivel = :n"
            ),
            {"u": usuario_id, "p": pipeline_id, "n": str(NIVEL)},
        )
    ).first() is not None


def _portas(sessoes: int, tokens: int) -> str:
    return f"{sessoes} sessions revoked, {tokens} pending tokens invalidated"


async def aplicar(
    *,
    email: str,
    nome: str,
    senha: str | None,
    perfil: str = Perfil.ESPECIALISTA,
    pipelines: list[str] | tuple[str, ...] = (),
    simular: bool = False,
    redefinir_senha: bool = False,
    ator: str | None = None,
    token_arquivo: str | Path | None = None,
    token_nome: str = ROTULO_PADRAO,
    token_dias: int = tokens_api.DIAS_PADRAO,
) -> list[str]:
    """Ensure the account, the token (`servico` profile) and the permissions; returns the report
    lines (never with a secret: of the token only the prefix appears, the same one the UI and the
    trail show).

    `ator` is the e-mail of the active admin who signs the trail: mandatory when writing; in a dry
    run, if given, it is checked the same way (a dry run that says "ok" and then fails on the real
    run is worse).

    Raises `ErroAplicacao` before any write when the input is unusable; and since everything runs
    in a single transaction, a failure midway undoes what came before, the token file included."""
    email = email.strip().lower()  # same rule as `usuarios.criar`: login is an exact match
    if not email:
        raise ErroAplicacao(codigo="email_invalido", mensagem="Provide the e-mail.", status=400)
    if perfil == Perfil.ADMIN:
        raise ErroAplicacao(
            codigo="perfil_recusado",
            mensagem="A service account cannot be admin: admin ignores fine-grained permissions "
            "and would trigger any pipeline.",
            status=400,
        )
    if perfil not in set(Perfil):
        raise ErroAplicacao(codigo="perfil_invalido", mensagem="Invalid profile.", status=400)
    servico = perfil == Perfil.SERVICO
    if servico and (senha is not None or redefinir_senha):
        raise ErroAplicacao(
            codigo="senha_nao_se_aplica",
            mensagem="Nothing was written: a service account has no password; it signs in with "
            "an API token (--token-file).",
            status=400,
        )
    caminho_token = Path(token_arquivo) if token_arquivo is not None else None
    if caminho_token is not None:
        _conferir_pedido_de_token(servico, caminho_token, token_nome, token_dias)
    if senha is not None:
        validar_politica(
            senha
        )  # before any write: a weak password must not create a passwordless account

    marca = "(dry run) " if simular else ""
    linhas: list[str] = []
    escritos: list[Path] = []
    try:
        async with transacao() as conn:
            ator_id = await _ator(conn, ator) if (not simular or ator) else None
            alvos = await _resolver_pipelines(conn, list(pipelines))
            existente = (
                await conn.execute(
                    text(
                        "SELECT id, perfil, ativo, senha_hash IS NULL AS sem_senha "
                        "FROM usuarios WHERE email = :e"
                    ),
                    {"e": email},
                )
            ).first()
            if existente is not None and existente.perfil != perfil:
                raise ErroAplicacao(
                    codigo="conta_divergente",
                    mensagem=f"Account {email} already exists with profile '{existente.perfil}', "
                    f"not '{perfil}'. The script does not change profiles; do that on the users "
                    "screen.",
                    status=409,
                )
            if existente is not None and not existente.ativo:
                raise ErroAplicacao(
                    codigo="conta_suspensa",
                    mensagem=f"Account {email} exists and is suspended. Re-enabling access is a "
                    "decision for whoever administers, through the users screen.",
                    status=409,
                )
            # Account created through the UI whose first password was never set: "kept" would mean
            # the script succeeded while the login answers 401. (A `servico` account has no
            # password at all.)
            sem_senha = not servico and existente is not None and bool(existente.sem_senha)
            precisa_senha = not servico and (existente is None or redefinir_senha or sem_senha)
            if precisa_senha and senha is None:
                if sem_senha:
                    raise ErroAplicacao(
                        codigo="conta_sem_senha",
                        mensagem=f"Account {email} exists WITHOUT a password (created through the "
                        "UI, first password pending): without one, its login answers 401. "
                        "Provide it with --password-file or --password-stdin. Nothing was written.",
                        status=400,
                    )
                raise ErroAplicacao(
                    codigo="senha_ausente",
                    mensagem="Provide the password with --password-file or --password-stdin.",
                    status=400,
                )

            if existente is None:
                feito = "would create" if simular else "created"
                linhas.append(f"account    {feito:12s} {email} ({perfil})")
                usuario_id = None
                if not simular:
                    # `ator_perfil` ADMIN is verified, not presumed: an `--actor` that is not an
                    # active admin was refused before any write.
                    criado = await usuarios.criar(
                        conn,
                        email=email,
                        nome=nome,
                        perfil=perfil,
                        ator_id=ator_id,
                        ator_perfil=Perfil.ADMIN,
                    )
                    usuario_id = criado.id
                    if not servico:
                        # `criar`'s token is the first-password one; the route sends it through
                        # the channel, here it is consumed right away, which is why nobody sees it.
                        await _definir_senha(
                            conn,
                            usuario_id,
                            senha,
                            ator_id,
                            token=criado.token_senha,
                            motivo="primeira_senha",
                        )
                if servico:
                    linhas.append(SENHA_NAO_SE_APLICA)
                else:
                    linhas.append(f"password   {'would set' if simular else 'set'}")
            else:
                usuario_id = existente.id
                linhas.append(f"account    {'exists':12s} {email} ({perfil})")
                if servico:
                    linhas.append(SENHA_NAO_SE_APLICA)
                elif sem_senha or redefinir_senha:
                    motivo = "primeira_senha_pendente" if sem_senha else "redefinicao"
                    if simular:
                        feito = "would set" if sem_senha else "would reset"
                        detalhe = "would revoke the account's sessions and pending tokens"
                    else:
                        feito = "set" if sem_senha else "reset"
                        detalhe = _portas(
                            *await _definir_senha(
                                conn, usuario_id, senha, ator_id, token=None, motivo=motivo
                            )
                        )
                    if sem_senha:
                        detalhe = f"the account existed without a password; {detalhe}"
                    linhas.append(f"password   {feito:12s} ({detalhe})")
                else:
                    linhas.append(
                        "password   kept         (the account already exists; "
                        "--reset-password changes it)"
                    )

            rotulo = (token_nome or "").strip()
            if caminho_token is not None and simular:
                linhas.append(
                    f"token      {'would issue':12s} '{rotulo}' ({token_dias} days) in "
                    f"{caminho_token} (new file, 0600)"
                )
            elif caminho_token is not None:
                emitido = await tokens_api.emitir(
                    conn, usuario_id=usuario_id, nome=rotulo, dias=token_dias, ator_id=ator_id
                )
                _gravar_token(caminho_token, emitido.token, escritos)
                vence = para_exibicao(emitido.expira_em).isoformat(timespec="minutes")
                linhas.append(
                    f"token      {'issued':12s} '{rotulo}' ({emitido.prefixo}..., expires "
                    f"{vence}) in {caminho_token} (0600)"
                )
            elif servico:
                linhas.append(
                    "token      none         (issue one on the users screen, API tokens, or with "
                    "--token-file)"
                )

            for nome_pp, pid in alvos:
                if usuario_id is not None and await _permissao_existe(conn, usuario_id, pid):
                    situacao = "existed"
                elif simular:
                    situacao = "would grant"
                else:
                    criou = await permissoes.conceder(
                        conn,
                        sujeito_tipo="usuario",
                        sujeito_id=usuario_id,
                        recurso_tipo="pipeline",
                        recurso_id=pid,
                        nivel=str(NIVEL),
                        concedido_por=ator_id,
                    )
                    situacao = "granted" if criou else "existed"
                    if criou:
                        # Same record as the `POST /permissoes` route (routes/permissoes.py): the
                        # trail must not depend on how the permission came in.
                        await registrar(
                            conn,
                            acao=Acao.CRIAR,
                            recurso_tipo="permissao",
                            recurso_id=pid,
                            ator_id=ator_id,
                            antes=None,
                            depois={
                                "sujeito_tipo": "usuario",
                                "sujeito_id": str(usuario_id),
                                "recurso_tipo": "pipeline",
                                "recurso_id": str(pid),
                                "nivel": str(NIVEL),
                            },
                        )
                linhas.append(f"permission {situacao:12s} {NIVEL} on pipeline '{nome_pp}' ({pid})")
    except BaseException:
        # All or nothing applies to the file too: the transaction undid the token, and a file
        # holding a token that does not exist in the database would only lead whoever uses it to
        # an unexplained 401.
        for caminho in escritos:
            with contextlib.suppress(OSError):
                caminho.unlink()
        raise

    if simular:
        linhas.append(f"\n{marca}nothing was written.")
    return linhas


def montar_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # No abbreviation: `--password` must not "match" `--password-file` by prefix.
        allow_abbrev=False,
    )
    ap.add_argument("--email", required=True, help="account e-mail (normalised to lower case)")
    ap.add_argument("--name", required=True, help="display name of the account")
    ap.add_argument(
        "--profile",
        default=str(Perfil.ESPECIALISTA),
        help="RBAC profile: servico (recommended: signs in only with a token and authors "
        "nothing) or especialista (the default; has a password). admin is refused",
    )
    fonte = ap.add_mutually_exclusive_group()
    fonte.add_argument("--password-file", metavar="PATH", help="read the password from this file")
    fonte.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input (no echo on a tty)",
    )
    # Exists only to REFUSE without echoing the value (see the module docstring).
    ap.add_argument("--password", help=argparse.SUPPRESS)
    ap.add_argument(
        "--token-file",
        metavar="PATH",
        help="(servico profile) issue an API token and write it to this NEW file, mode 0600, "
        "never to stdout",
    )
    ap.add_argument(
        "--token-name",
        default=ROTULO_PADRAO,
        metavar="NAME",
        help=f"token name: where it will live (default: '{ROTULO_PADRAO}')",
    )
    ap.add_argument(
        "--token-days",
        type=int,
        default=tokens_api.DIAS_PADRAO,
        metavar="N",
        help=f"token lifetime, from 1 to {tokens_api.DIAS_MAX} days "
        f"(default: {tokens_api.DIAS_PADRAO})",
    )
    ap.add_argument(
        "--execute",
        action="append",
        default=[],
        metavar="PIPELINE",
        help="name of a pipeline the account may execute (repeatable)",
    )
    ap.add_argument(
        "--reset-password",
        action="store_true",
        help="change the password of an account that already exists (revokes its sessions)",
    )
    ap.add_argument(
        "--actor",
        metavar="EMAIL",
        help="e-mail of the active admin who signs the trail (mandatory without --dry-run)",
    )
    ap.add_argument("--dry-run", action="store_true", help="show the plan; write nothing")
    return ap


def ler_senha(args: argparse.Namespace) -> str | None:
    """Password from the file or standard input. Strips only the trailing line break: the rest
    (spaces included) belongs to the password, and trimming more would create a password different
    from the one in the file."""
    if args.password_file:
        return Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
    if args.password_stdin:
        if sys.stdin.isatty():
            return getpass.getpass("Service account password: ")
        return sys.stdin.readline().rstrip("\r\n")
    return None


def _imprimir_erro(exc: ErroAplicacao) -> None:
    print(f"error [{exc.codigo}]: {exc.mensagem}", file=sys.stderr)
    for d in exc.detalhes:
        print(f"  - {d.campo}: {d.mensagem}", file=sys.stderr)


async def principal(argv: list[str] | None = None) -> int:
    ap = montar_parser()
    args = ap.parse_args(argv)
    if args.password is not None:
        ap.error(
            "a password as an argument is refused: it leaks into `ps` and shell history. "
            "Use --password-file <path> or --password-stdin."
        )
    try:
        senha = ler_senha(args)
    except OSError as exc:  # only the OS reason, never the file content
        print(f"error: could not read the password file ({exc.strerror}).", file=sys.stderr)
        return 1
    try:
        linhas = await aplicar(
            email=args.email,
            nome=args.name,
            senha=senha,
            perfil=args.profile,
            pipelines=args.execute,
            simular=args.dry_run,
            redefinir_senha=args.reset_password,
            ator=args.actor,
            token_arquivo=args.token_file,
            token_nome=args.token_name,
            token_dias=args.token_days,
        )
    except ErroAplicacao as exc:
        _imprimir_erro(exc)
        return 1
    for linha in linhas:
        print(linha)
    return 0


def main() -> int:
    return asyncio.run(principal())


if __name__ == "__main__":
    sys.exit(main())
