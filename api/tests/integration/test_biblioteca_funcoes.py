"""Function library: versioned DuckDB MACROs (draft != published, audited).

Reuses the generic CRUD (`cflow_core.modelagem.versionado`) with this entity's tables. Here we
exercise the service/versionado + `definicao.validar` + `compilar.montar_ddl` directly inside
`transacao()`; the HTTP routes are covered in `test_funcoes_rotas.py`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.authz import permissoes
from cflow_core.biblioteca_funcoes import compilar, definicao, service
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.modelagem import versionado

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

_T = {
    "tabela": "biblioteca_funcoes",
    "versoes": "biblioteca_funcoes_versoes",
    "idcol": "biblioteca_id",
    "recurso_tipo": "biblioteca_funcao",
}

# Example library: identifier handling.
DEF_LIB = {
    "funcoes": [
        {
            "nome": "digits_only",
            "args": ["c"],
            "corpo": "regexp_replace(c, '[^0-9]', '', 'g')",
            "descricao": "Removes everything that is not a digit",
        },
        {"nome": "now_ts", "args": [], "corpo": "current_timestamp"},
    ]
}


async def _especialista(owner_engine, email):
    return await semear_usuario(owner_engine, email=email, perfil="especialista")


async def _criar(conn, dono_id, nome="Identifiers"):
    return await versionado.criar(
        conn,
        tabela=_T["tabela"],
        recurso_tipo=_T["recurso_tipo"],
        nome=nome,
        descricao=None,
        dono_id=dono_id,
    )


async def _rascunho(conn, rid, dono, base_numero=0):
    return await versionado.salvar_rascunho(
        conn,
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=_T["recurso_tipo"],
        recurso_id=rid,
        definicao=DEF_LIB,
        base_numero=base_numero,
        autor_id=dono,
        validar=definicao.validar,
    )


async def _publicar(conn, rid, dono, base_numero=1):
    return await versionado.publicar(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=_T["recurso_tipo"],
        recurso_id=rid,
        base_numero=base_numero,
        nota="v1",
        autor_id=dono,
        validar=definicao.validar,
    )


# --------------------------- lifecycle (versioned) ---------------------------


async def test_criar_rascunho_publicar_obter(owner_engine, app_engine):
    dono = await _especialista(owner_engine, "bf-dono@example.com")
    async with transacao() as conn:
        rid = await _criar(conn, dono)
        assert await _rascunho(conn, rid, dono) == 1
        assert await _publicar(conn, rid, dono) == 2
        obtido = await versionado.obter(
            conn, tabela=_T["tabela"], versoes=_T["versoes"], idcol=_T["idcol"], recurso_id=rid
        )
    assert obtido["nome"] == "Identifiers"
    assert obtido["definicao"]["funcoes"][0]["nome"] == "digits_only"

    # published version resolved through the pointer (thin service)
    async with app_engine.connect() as conn:
        pubs = await service.obter_publicada(conn, [rid])
        pin = await service.resolver_pin(conn, [rid])
    assert pubs == [{"id": str(rid), "numero": 2, "definicao": DEF_LIB}]
    assert pin == [{"id": str(rid), "numero": 2}]


async def test_obter_publicada_ignora_sem_publicacao(owner_engine, app_engine):
    dono = await _especialista(owner_engine, "bf-semepub@example.com")
    async with transacao() as conn:
        rid = await _criar(conn, dono)
        await _rascunho(conn, rid, dono)  # draft, NOT published
    async with app_engine.connect() as conn:
        assert await service.obter_publicada(conn, [rid]) == []
        assert await service.resolver_pin(conn, [rid]) == []


# --------------------------- audit (same transaction) ---------------------------


async def test_auditoria_criar_publicar_excluir(owner_engine, app_engine):
    dono = await _especialista(owner_engine, "bf-aud@example.com")
    async with transacao() as conn:
        rid = await _criar(conn, dono)
        await _rascunho(conn, rid, dono)
        await _publicar(conn, rid, dono)

    async with owner_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, depois FROM auditoria WHERE recurso_tipo='biblioteca_funcao' "
                        "AND recurso_id = :r ORDER BY id"
                    ),
                    {"r": rid},
                )
            )
            .mappings()
            .all()
        )
    acoes = [x["acao"] for x in linhas]
    assert acoes == ["criar", "editar", "publicar"]
    assert linhas[0]["depois"]["nome"] == "Identifiers"  # diff do criar

    # delete -> removes versions/permissions + writes exactly one delete record
    async with transacao() as conn:
        await versionado.excluir(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=rid,
            ator_id=dono,
        )
    async with owner_engine.connect() as conn:
        n_ver = (
            await conn.execute(
                text("SELECT count(*) FROM biblioteca_funcoes_versoes WHERE biblioteca_id = :r"),
                {"r": rid},
            )
        ).scalar_one()
        n_aud = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='excluir' "
                    "AND recurso_tipo='biblioteca_funcao' AND recurso_id = :r"
                ),
                {"r": rid},
            )
        ).scalar_one()
    assert n_ver == 0 and n_aud == 1


# --------------------------- authorization (per-resource edge) ---------------------------


async def test_authz_dono_admin_e_intruso(owner_engine, app_engine):
    dono = await _especialista(owner_engine, "bf-d2@example.com")
    intruso = await _especialista(owner_engine, "bf-intruso@example.com")
    admin = await semear_usuario(owner_engine, email="bf-admin@example.com", perfil="admin")
    async with transacao() as conn:
        rid = await _criar(conn, dono)  # criador recebe 'administrar'

    async with app_engine.connect() as conn:
        # owner administers/edits (does not raise)
        await permissoes.exigir(conn, dono, _T["recurso_tipo"], rid, "administrar")
        await permissoes.exigir(conn, dono, _T["recurso_tipo"], rid, "editar")
        # admin passes through the profile bypass
        await permissoes.exigir(conn, admin, _T["recurso_tipo"], rid, "administrar")
        # intruder (specialist without permission on the resource) -> generic 403
        with pytest.raises(ErroAplicacao) as ei:
            await permissoes.exigir(conn, intruso, _T["recurso_tipo"], rid, "ver")
        assert ei.value.status == 403 and ei.value.codigo == "acesso_negado"


# --------------------------- definition validation (422) ---------------------------


@pytest.mark.parametrize(
    "d, motivo",
    [
        ({"funcoes": []}, "empty funcoes"),
        ({}, "missing funcoes"),
        ({"funcoes": [{"nome": "1bad", "args": [], "corpo": "1"}]}, "invalid name"),
        ({"funcoes": [{"nome": "ok", "args": ["1x"], "corpo": "1"}]}, "invalid arg"),
        ({"funcoes": [{"nome": "f", "args": [], "corpo": ""}]}, "empty corpo"),
        ({"funcoes": [{"nome": "f", "args": [], "corpo": "   "}]}, "whitespace-only body"),
        (
            {"funcoes": [{"nome": "f", "args": [], "corpo": "1", "retorno": "lista"}]},
            "invalid retorno",
        ),
        (
            {
                "funcoes": [
                    {"nome": "dup", "args": [], "corpo": "1"},
                    {"nome": "dup", "args": [], "corpo": "2"},
                ]
            },
            "duplicate name",
        ),
    ],
)
def test_validar_recusa(d, motivo):
    with pytest.raises(ErroAplicacao) as e:
        definicao.validar(d)
    assert e.value.status == 422 and e.value.codigo == "definicao_invalida", motivo


def test_validar_aceita_struct_e_descricao():
    d = {
        "funcoes": [
            {
                "nome": "par",
                "args": ["a", "b"],
                "corpo": "struct_pack(x := a, y := b)",
                "retorno": "struct",
            },
            {"nome": "clean", "args": ["c"], "corpo": "trim(c)", "descricao": "trims spaces"},
        ]
    }
    definicao.validar(d)  # does not raise


# --------------------------- compilation (DDL + collision) ---------------------------


def test_montar_ddl_amostra():
    ddl = compilar.montar_ddl([DEF_LIB])
    assert ddl == (
        "CREATE OR REPLACE MACRO digits_only(c) AS (regexp_replace(c, '[^0-9]', '', 'g'));\n"
        "CREATE OR REPLACE MACRO now_ts() AS (current_timestamp);"
    )


def test_montar_ddl_vazio():
    assert compilar.montar_ddl([]) == ""


def test_montar_ddl_struct_emite_macro():
    d = {
        "funcoes": [
            {"nome": "p", "args": ["a"], "corpo": "struct_pack(x := a)", "retorno": "struct"},
        ]
    }
    assert compilar.montar_ddl([d]) == "CREATE OR REPLACE MACRO p(a) AS (struct_pack(x := a));"


def test_montar_ddl_colisao_409():
    a = {"funcoes": [{"nome": "clean", "args": ["c"], "corpo": "trim(c)"}]}
    b = {"funcoes": [{"nome": "clean", "args": ["c"], "corpo": "lower(c)"}]}
    with pytest.raises(ErroAplicacao) as e:
        compilar.montar_ddl([a, b])
    assert e.value.status == 409 and e.value.codigo == "macro_colisao"
    assert "clean" in e.value.mensagem
