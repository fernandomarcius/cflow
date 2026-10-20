"""SSO/OIDC: validates the ID token (RS256/JWKS) and provisions/signs in the local user.

Uses a simulated IdP: a local RS256 keypair + a JWKS endpoint. It exercises the real OIDC path
(signature, kid/JWKS, issuer, audience) deterministically, without starting a Keycloak.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

pytestmark = pytest.mark.integration

ISSUER = "https://idp.example.com/realms/acme"
CLIENT = "cflow"
KID = "k1"


@pytest.fixture
def idp():
    """Start a local JWKS and return the private key."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    corpo = json.dumps({"keys": [jwk]}).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    porta = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # point SSO at the simulated IdP
    from cflow_core.config import get_settings

    os.environ["CFLOW_OIDC_ISSUER"] = ISSUER
    os.environ["CFLOW_OIDC_CLIENT_ID"] = CLIENT
    os.environ["CFLOW_OIDC_JWKS_URI"] = f"http://127.0.0.1:{porta}/jwks"
    get_settings.cache_clear()

    yield priv

    srv.shutdown()
    for k in ("CFLOW_OIDC_ISSUER", "CFLOW_OIDC_CLIENT_ID", "CFLOW_OIDC_JWKS_URI"):
        os.environ.pop(k, None)
    get_settings.cache_clear()


def _token(priv, email: str, **extra) -> str:
    agora = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT,
        "iat": agora,
        "exp": agora + 300,
        "email": email,
        **extra,
    }
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": KID})


async def test_sso_provisiona_e_loga(app_engine, cliente, idp):
    tok = _token(idp, "sso@example.com", name="Jane SSO")
    r = await cliente.post("/auth/sso/oidc", json={"id_token": tok})
    assert r.status_code == 200 and r.json()["token"]

    async with app_engine.connect() as conn:
        u = (
            await conn.execute(
                text("SELECT perfil, fonte_auth, ativo FROM usuarios WHERE email='sso@example.com'")
            )
        ).first()
    assert u is not None and u.fonte_auth == "oidc" and u.perfil == "operador" and u.ativo is True


async def test_sso_reusa_usuario_existente(app_engine, cliente, idp):
    await cliente.post("/auth/sso/oidc", json={"id_token": _token(idp, "rep@example.com")})
    await cliente.post("/auth/sso/oidc", json={"id_token": _token(idp, "rep@example.com")})
    async with app_engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM usuarios WHERE email='rep@example.com'"))
        ).scalar_one()
    assert n == 1  # does not duplicate


async def test_sso_token_invalido(app_engine, cliente, idp):
    # token signed by ANOTHER key -> invalid signature
    outra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = outra.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    ruim = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT, "exp": int(time.time()) + 300, "email": "x@example.com"},
        pem,
        algorithm="RS256",
        headers={"kid": KID},
    )
    r = await cliente.post("/auth/sso/oidc", json={"id_token": ruim})
    assert r.status_code == 401
