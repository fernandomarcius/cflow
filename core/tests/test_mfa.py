"""Unit: TOTP (RFC 6238)."""

from __future__ import annotations

from cflow_core.auth import mfa


def test_totp_roundtrip():
    s = mfa.gerar_segredo()
    assert mfa.verificar_totp(s, mfa.totp_atual(s))
    assert not mfa.verificar_totp(s, "000000")


def test_tolera_janela():
    s = mfa.gerar_segredo()
    # the previous step code is still accepted (window of +/-1)
    anterior = mfa.totp_atual(s, momento=0)
    assert mfa.verificar_totp(s, anterior, momento=30)


def test_uri():
    u = mfa.uri_provisionamento("a@example.com", "ABC123")
    assert u.startswith("otpauth://totp/") and "secret=ABC123" in u
