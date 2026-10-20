"""Structural validation of the graph."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

FONTE = {"id": "a", "tipo": "sql", "sql": "SELECT 1"}


async def _pipeline(cliente, h) -> str:
    return (await cliente.post("/pipelines", json={"nome": "p"}, headers=h)).json()["id"]


async def test_grafo_valido_salva(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="e1@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h)
    definicao = {
        "nos": [
            {"id": "a", "tipo": "sql", "sql": "SELECT 1"},
            {"id": "b", "tipo": "sql", "sql": "SELECT * FROM a"},
        ],
        "arestas": [{"de": "a", "para": "b"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200


async def test_ciclo_rejeitado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="e2@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h)
    definicao = {
        "nos": [
            {"id": "a", "tipo": "sql", "sql": "SELECT 1"},
            {"id": "b", "tipo": "sql", "sql": "SELECT 1"},
        ],
        "arestas": [{"de": "a", "para": "b"}, {"de": "b", "para": "a"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422


async def test_referencia_quebrada_rejeitada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="e3@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h)
    definicao = {
        "nos": [{"id": "a", "tipo": "sql", "sql": "SELECT 1"}],
        "arestas": [{"de": "a", "para": "ghost"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422


async def test_no_desconectado_rejeitado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="e4@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h)
    definicao = {
        "nos": [
            {"id": "a", "tipo": "sql", "sql": "SELECT 1"},
            {"id": "loose", "tipo": "sql", "sql": "SELECT 2"},
            {"id": "b", "tipo": "sql", "sql": "SELECT 1"},
        ],
        "arestas": [{"de": "a", "para": "b"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
