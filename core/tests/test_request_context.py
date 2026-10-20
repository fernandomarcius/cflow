"""Unit: request_id context."""

from __future__ import annotations

import uuid

from cflow_core.request_context import (
    definir_request_id,
    novo_request_id,
    request_id_atual,
)


def test_novo_request_id_e_uuid():
    rid = novo_request_id()
    uuid.UUID(rid)  # does not raise -> it is a valid UUID


def test_definir_usa_valor_fornecido():
    assert definir_request_id("fixo-123") == "fixo-123"
    assert request_id_atual() == "fixo-123"


def test_definir_gera_quando_ausente():
    rid = definir_request_id(None)
    assert rid
    assert request_id_atual() == rid
