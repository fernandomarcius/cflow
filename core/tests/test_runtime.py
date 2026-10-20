"""Runtime helpers (cflow_core.runtime): file-mounted secrets and TLS for the metadata Postgres."""

from __future__ import annotations

import datetime as dt
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy.engine import make_url

from cflow_core import runtime
from cflow_core.config import Settings, get_settings
from cflow_core.runtime import contexto_ssl, exportar_segredos_arquivo, nome_variavel


@pytest.fixture
def ca_pem(tmp_path):
    chave = ec.generate_private_key(ec.SECP256R1())
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca-teste")])
    agora = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(minutes=1))
        .not_valid_after(agora + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(chave, hashes.SHA256())
    )
    caminho = tmp_path / "ca.crt"
    caminho.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(caminho)


@pytest.fixture
def ambiente_limpo(monkeypatch, tmp_path):
    """No TLS, no secrets directory, and the settings singleton reset before and after."""
    for chave in ("CFLOW_DATABASE_TLS", "CFLOW_DATABASE_CA_FILE", "CFLOW_DATABASE_SCHEMA"):
        monkeypatch.delenv(chave, raising=False)
    monkeypatch.setattr(runtime, "DIRETORIO_SEGREDOS", str(tmp_path / "no-secrets"))
    monkeypatch.delenv("CFLOW_SECRETS_DIR", raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("diretorio", "esperado"),
    [
        ("cflow-vault-key", "CFLOW_VAULT_KEY"),
        ("CFLOW_SMTP_PASSWORD", "CFLOW_SMTP_PASSWORD"),
        ("1-starts-with-digit", None),
        ("has.dot", None),
    ],
)
def test_nome_variavel(diretorio, esperado):
    assert nome_variavel(diretorio) == esperado


def test_exporta_segredos_sem_sobrescrever(tmp_path):
    for mount, valor in {
        "cflow-vault-key": "key-from-file\n",
        "cflow-admin-password": "other",
        "invalid.name": "x",
    }.items():
        (tmp_path / mount).mkdir()
        (tmp_path / mount / "value").write_text(valor)
    env = {"CFLOW_ADMIN_PASSWORD": "already-set"}

    exportadas = exportar_segredos_arquivo(tmp_path, env)

    assert exportadas == ["CFLOW_VAULT_KEY"]
    assert env["CFLOW_VAULT_KEY"] == "key-from-file"  # trailing newline removed
    assert env["CFLOW_ADMIN_PASSWORD"] == "already-set"  # an existing variable wins over the file
    assert "INVALID.NAME" not in env


def test_sem_diretorio_de_segredos_nao_faz_nada(tmp_path):
    env: dict[str, str] = {}
    assert exportar_segredos_arquivo(tmp_path / "does-not-exist", env) == []
    assert env == {}


def test_segredo_grande_demais_e_ignorado(tmp_path):
    (tmp_path / "cflow-big").mkdir()
    (tmp_path / "cflow-big" / "value").write_bytes(b"x" * (runtime.TAMANHO_MAX_SEGREDO + 1))
    env: dict[str, str] = {}
    assert exportar_segredos_arquivo(tmp_path, env) == []
    assert env == {}


def test_diretorio_de_segredos_vem_do_ambiente(ambiente_limpo, tmp_path):
    raiz = tmp_path / "secrets"
    (raiz / "cflow-vault-key").mkdir(parents=True)
    (raiz / "cflow-vault-key" / "value").write_text("fernet-key")
    ambiente_limpo.setenv("CFLOW_SECRETS_DIR", str(raiz))
    ambiente_limpo.delenv("CFLOW_VAULT_KEY", raising=False)
    assert get_settings().vault_key == "fernet-key"


def test_contexto_ssl_com_ca_verifica_a_cadeia(ca_pem):
    ctx = contexto_ssl(ca_pem)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_contexto_ssl_sem_ca_so_cifra():
    ctx = contexto_ssl(None)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_engine_de_runtime_recebe_o_contexto_tls(ambiente_limpo, ca_pem):
    from cflow_core import db

    ambiente_limpo.setenv("CFLOW_DATABASE_TLS", "true")
    ambiente_limpo.setenv("CFLOW_DATABASE_CA_FILE", ca_pem)
    ambiente_limpo.setenv("CFLOW_DATABASE_SCHEMA", "app, public")
    capturado: dict = {}

    def falso(url, **kw):
        capturado.update(url=url, **kw)
        return object()

    ambiente_limpo.setattr(db, "create_async_engine", falso)
    db._limpar_engines()
    try:
        db.get_engine()
    finally:
        db._limpar_engines()
    assert isinstance(capturado["connect_args"]["ssl"], ssl.SSLContext)
    assert capturado["connect_args"]["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert capturado["connect_args"]["server_settings"] == {"search_path": "app, public"}


def test_engine_de_runtime_sem_tls_nao_muda(ambiente_limpo):
    from cflow_core import db

    ambiente_limpo.setenv("CFLOW_DATABASE_URL", "postgresql+asyncpg://a:b@h:5432/cflow")
    capturado: dict = {}
    ambiente_limpo.setattr(db, "create_async_engine", lambda url, **kw: capturado.update(kw))
    db._limpar_engines()
    try:
        db.get_engine()
    finally:
        db._limpar_engines()
    assert "ssl" not in capturado["connect_args"]
    assert "server_settings" not in capturado["connect_args"]


def test_catalogo_iceberg_recebe_o_contexto_tls(ambiente_limpo, ca_pem):
    import sqlalchemy

    from cflow_core.ingestao import iceberg

    ambiente_limpo.setenv("CFLOW_DATABASE_TLS", "true")
    ambiente_limpo.setenv("CFLOW_DATABASE_CA_FILE", ca_pem)
    ambiente_limpo.setenv("CFLOW_DATABASE_SCHEMA", "app, public")
    capturado: dict = {}
    ambiente_limpo.setattr(
        sqlalchemy, "create_engine", lambda url, **kw: capturado.update(url=url, **kw) or "new"
    )

    class _Engine:
        descartada = False

        def dispose(self):
            self.descartada = True

    class _Catalogo:
        engine = _Engine()

    cat = _Catalogo()
    antiga = cat.engine
    iceberg._aplicar_tls(cat)
    assert antiga.descartada and cat.engine == "new"
    assert make_url(capturado["url"]).drivername == "postgresql+pg8000"
    assert isinstance(capturado["connect_args"]["ssl_context"], ssl.SSLContext)
    assert capturado["connect_args"]["startup_params"] == {"search_path": "app, public"}


def test_sem_schema_configurado_nao_ha_search_path(ambiente_limpo):
    ambiente_limpo.setenv("CFLOW_DATABASE_URL", "postgresql+asyncpg://a:b@h:5432/cflow")
    assert Settings(_env_file=None).database_schema is None
