"""PII tokenization on write: the tax ID must not reach the published data.

What makes this feature viable is the **distinct-values cache**: the KMS `MacSign` API is one call
per value, and real data has hundreds of thousands of non-empty cells but only a few thousand
distinct values. Tokenizing cell by cell would be unworkable. That is why a test counts calls:
replacing the map with a per-row call would go unnoticed by every content assertion and kill the
feature in production.

Nothing here touches the network: the test tokenizer is a double that records what it received.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import tokenizacao as tk


class TokenizadorFalso:
    """Records the values it received, which allows counting calls and proving the cache."""

    def __init__(self) -> None:
        self.lotes: list[list[str]] = []

    async def mapear(self, valores: list[str]) -> dict[str, str]:
        self.lotes.append(list(valores))
        return {v: f"{tk.PREFIXO}{i:032x}" for i, v in enumerate(valores)}

    @property
    def chamadas_de_valor(self) -> int:
        return sum(len(lote) for lote in self.lotes)


def _cadastro(ids: list[str | None]) -> pa.Table:
    return pa.table({"tax_id_owner": pa.array(ids, type=pa.string()), "name": ["x"] * len(ids)})


async def test_a_coluna_classificada_e_substituida() -> None:
    t = await tk.tokenizar_tabela(
        dataset="registry", tabela=_cadastro(["11111111111"]), tokenizador=TokenizadorFalso()
    )
    valor = t.column("tax_id_owner").to_pylist()[0]
    assert valor.startswith(tk.PREFIXO)
    assert "11111111111" not in valor, "the identifier must not survive in the published data"


async def test_coluna_nao_classificada_passa_intacta() -> None:
    t = await tk.tokenizar_tabela(
        dataset="registry", tabela=_cadastro(["11111111111"]), tokenizador=TokenizadorFalso()
    )
    assert t.column("name").to_pylist() == ["x"]


async def test_o_mesmo_valor_recebe_o_MESMO_token() -> None:
    """The only use of a token: joining records of the same person without knowing who they are."""
    t = await tk.tokenizar_tabela(
        dataset="registry",
        tabela=_cadastro(["111", "222", "111"]),
        tokenizador=TokenizadorFalso(),
    )
    a, b, c = t.column("tax_id_owner").to_pylist()
    assert a == c and a != b


async def test_so_os_DISTINTOS_vao_ao_KMS() -> None:
    """The whole viability is here: 6 rows, 2 distinct values, 2 calls."""
    fake = TokenizadorFalso()
    await tk.tokenizar_tabela(
        dataset="registry",
        tabela=_cadastro(["111", "222", "111", "222", "111", "222"]),
        tokenizador=fake,
    )
    assert fake.chamadas_de_valor == 2, (
        "one call per ROW makes tokenization unworkable in production (real data has far more "
        "non-empty cells than distinct values)"
    )


async def test_um_mapa_para_a_tabela_INTEIRA_cruza_entre_colunas() -> None:
    """The same ID in two columns must get the same token, otherwise it cannot be joined.

    `orders_detail` has 18 tax-ID columns for parties; the same party appears in several.
    """
    fake = TokenizadorFalso()
    tab = pa.table(
        {
            "tab_a_tax_id_party_1": ["999"],
            "tab_a_tax_id_party_2": ["999"],
            "tab_b_tax_id_party_1": ["888"],
        }
    )
    t = await tk.tokenizar_tabela(dataset="orders_detail", tabela=tab, tokenizador=fake)
    assert len(fake.lotes) == 1, "one map per table, not one per column"
    assert fake.chamadas_de_valor == 2
    a = t.column("tab_a_tax_id_party_1").to_pylist()[0]
    b = t.column("tab_a_tax_id_party_2").to_pylist()[0]
    assert a == b


async def test_NULL_continua_NULL_e_vazio_continua_vazio() -> None:
    """ "Not provided" and "provided X" are different findings: tokenizing absence invents someone."""
    t = await tk.tokenizar_tabela(
        dataset="registry",
        tabela=_cadastro([None, "", "  ", "111"]),
        tokenizador=TokenizadorFalso(),
    )
    a, b, c, d = t.column("tax_id_owner").to_pylist()
    assert a is None and b is None and c is None
    assert d.startswith(tk.PREFIXO)


async def test_SEM_tokenizador_a_publicacao_e_RECUSADA() -> None:
    """Air-gapped is no license to write in clear: it is a refusal, naming the columns."""
    with pytest.raises(ErroAplicacao) as e:
        await tk.tokenizar_tabela(dataset="registry", tabela=_cadastro(["111"]), tokenizador=None)
    assert e.value.codigo == "pii_sem_tokenizador"
    assert "tax_id_owner" in e.value.mensagem, "the message must say WHICH column"


async def test_dataset_SEM_PII_publica_sem_tokenizador() -> None:
    """Datasets without PII keep running air-gapped."""
    tab = pa.table({"qualquer": ["a"]})
    assert await tk.tokenizar_tabela(dataset="rates_daily", tabela=tab, tokenizador=None) is tab


async def test_coluna_declarada_e_ausente_no_schema_nao_quebra() -> None:
    """The silver schema changes as typing advances; the list must not fail because of that."""
    tab = pa.table({"tab_a_tax_id_party_1": ["777"]})
    t = await tk.tokenizar_tabela(
        dataset="orders_detail", tabela=tab, tokenizador=TokenizadorFalso()
    )
    assert t.column("tab_a_tax_id_party_1").to_pylist()[0].startswith(tk.PREFIXO)


async def test_colunas_de_lista_so_as_presentes() -> None:
    esquema = pa.schema([("tab_a_tax_id_party_1", pa.string()), ("outra", pa.string())])
    assert tk.colunas_de("orders_detail", esquema) == ["tab_a_tax_id_party_1"]
    assert tk.colunas_de("rates_daily", esquema) == []


def test_o_recorte_de_pii_e_explicito() -> None:
    """Explicit record of the scope: 19 tax-ID columns are tokenized; contact and address columns
    stay out by product decision.

    Tokenizing a postal code destroys regional aggregation, and a few hundred distinct values are
    trivially brute-forceable even with a key. Person NAMES are not numeric identifiers either.

    The test exists so that widening the scope is a decision, not a side effect.
    """
    assert sum(len(v) for v in tk.COLUNAS_PII.values()) == 19
    assert "contacts" not in tk.COLUNAS_PII
    assert tk.COLUNAS_PII["registry"] == ("tax_id_owner",)


# ---------------------------------------------------------------------------------------------
# The two defects that only showed up against a real KMS: no unit test would catch them, because
# both sit on the BOUNDARY (URL and token scope), not in the logic.
# ---------------------------------------------------------------------------------------------

CHAVE = "projects/p/locations/us-east1/keyRings/kr/cryptoKeys/datalake-pii-token"
VERSAO = f"{CHAVE}/cryptoKeyVersions/1"


class _LakeFalso:
    def __init__(self) -> None:
        self.pedidos: list[str] = []

    async def token_para_kms(self) -> str:
        self.pedidos.append("kms")
        return "token-kms"

    async def token_para_terceiros(self) -> tuple[str, int]:
        self.pedidos.append("storage")
        return "token-storage", 0


async def test_apontar_para_a_CHAVE_e_nao_para_a_versao_e_recusado(monkeypatch) -> None:
    """`macSign` signs by VERSION. Pointing at the key returns an HTML 404 from the Google
    frontend, which does not even look like an API error and would send whoever configured it
    hunting for a permission problem."""
    monkeypatch.setattr(tk.get_settings(), "pii_chave_kms", CHAVE)
    with pytest.raises(ErroAplicacao) as e:
        await tk.abrir_tokenizador(_LakeFalso())
    assert e.value.codigo == "pii_chave_kms_invalida"
    assert "cryptoKeyVersions" in e.value.mensagem


async def test_pede_o_token_de_KMS_e_nao_o_de_STORAGE(monkeypatch) -> None:
    """The `token_para_terceiros` token has `devstorage.read_write` scope, and KMS answers it with
    `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`, an error that looks like missing permission and is not."""
    monkeypatch.setattr(tk.get_settings(), "pii_chave_kms", VERSAO)
    lake = _LakeFalso()
    t = await tk.abrir_tokenizador(lake)
    assert t is not None
    assert lake.pedidos == ["kms"], "asked for the wrong token: KMS rejects the storage scope"


async def test_sem_chave_configurada_nao_ha_tokenizador(monkeypatch) -> None:
    """`None` is no license to publish in clear: `tokenizar_tabela` is what refuses later."""
    monkeypatch.setattr(tk.get_settings(), "pii_chave_kms", "")
    assert await tk.abrir_tokenizador(_LakeFalso()) is None
