"""The columnar direct read (`acelerar_leitura`) produces the SAME text as `texto()` over the value
that the original `pytds` `read` would build from the same bytes, on the fast path (whole value in
the packet) and on the boundary path (value crossing the packet, which falls back to the original
`read`)."""

import codecs
import random
import struct
from types import SimpleNamespace

import pytest
from pytds.collate import raw_collation
from pytds.tds_types import (
    BitNSerializer,
    BitType,
    DateTimeNSerializer,
    IntNSerializer,
    MsDecimalSerializer,
    NVarChar72Serializer,
    NVarCharMaxSerializer,
    VarChar72Serializer,
)

from cflow_core.conexoes import colunar as C


class _Reader:
    """Minimal TDS stream with the packet buffer (`_buf/_pos/_size`) and the primitives the original
    `read` methods use. `pacote` < data size simulates the boundary: the fast path cannot see the
    end of the value, and the primitives keep reading (as if the next packet had arrived)."""

    def __init__(self, dados: bytes, pacote: int | None = None):
        self._buf = bytearray(dados)
        self._pos = 0
        self._size = len(dados) if pacote is None else pacote
        self._session = SimpleNamespace(
            _tds=SimpleNamespace(_login=SimpleNamespace(bytes_to_unicode=True)), tzinfo_factory=None
        )

    def recv(self, n):
        pedaco = bytes(self._buf[self._pos : self._pos + n])
        self._pos += len(pedaco)
        self._size = max(self._size, self._pos)
        return pedaco

    def get_byte(self):
        return self.recv(1)[0]

    def get_smallint(self):
        return struct.unpack("<h", self.recv(2))[0]

    def get_usmallint(self):
        return struct.unpack("<H", self.recv(2))[0]

    def read_str(self, size, codec):
        return codec.decode(self.recv(size))[0]

    def unpack(self, struc):
        return struc.unpack(self.recv(struc.size))

    def get_int(self):
        return self.unpack(struct.Struct("<l"))[0]

    def get_int8(self):
        return self.unpack(struct.Struct("<q"))[0]

    @property
    def session(self):
        return SimpleNamespace(tzinfo_factory=None, bad_stream=lambda *a: ValueError(a))


def _cursor(serializadores, unicode_=True, fuso=None):
    cols = [SimpleNamespace(serializer=s) for s in serializadores]
    login = SimpleNamespace(bytes_to_unicode=unicode_)
    return SimpleNamespace(
        _session=SimpleNamespace(
            res_info=SimpleNamespace(columns=cols),
            _tds=SimpleNamespace(_login=login),
            tzinfo_factory=fuso,
        )
    )


def _par(fabrica, **kw):
    """(original, accelerated): two identical instances, only the second one swapped."""
    original, rapido = fabrica(**kw), fabrica(**kw)
    assert C.acelerar_leitura(_cursor([rapido])) == 1
    return original, rapido


def _conferir(original, rapido, dados):
    esperado = C.texto(original.read(_Reader(dados)))
    assert rapido.read(_Reader(dados)) == esperado
    if len(dados) > 1:
        # Boundary: the current packet ends in the middle of the value.
        assert rapido.read(_Reader(dados, pacote=len(dados) // 2)) == esperado


rnd = random.Random(20260928)


def _decimal_bytes(precisao, n, positivo):
    t = MsDecimalSerializer._bytes_per_prec[precisao]
    return bytes([t, 1 if positivo else 0]) + n.to_bytes(t - 1, "little")


@pytest.mark.parametrize(
    "precisao,escala", [(18, 2), (18, 0), (28, 4), (38, 10), (38, 0), (9, 9), (5, 2)]
)
def test_decimal(precisao, escala):
    original, rapido = _par(MsDecimalSerializer, precision=precisao, scale=escala)
    valores = [0, 1, 10**precisao - 1] + [rnd.randrange(10**precisao) for _ in range(1500)]
    for n in valores:
        for positivo in (True, False):
            _conferir(original, rapido, _decimal_bytes(precisao, n, positivo))
    assert rapido.read(_Reader(bytes([0]))) is None
    assert rapido.read(_Reader(_decimal_bytes(18, 0, False) if precisao == 18 else bytes([0]))) in (
        "-0.0",
        None,
    )


TEXTOS = [
    "",
    "a",
    "Crystal Waters",
    "caf\u00e9 \u00c7 \u00f1 \u00fc",
    "x" * 400,
    "  padded  ",
    "1_80_FORM",
    "€",
]


def test_nvarchar():
    original, rapido = _par(NVarChar72Serializer, size=4000, collation=raw_collation)
    for t in TEXTOS + [
        "".join(chr(rnd.randrange(32, 0x2FFF)) for _ in range(rnd.randrange(60)))
        for _ in range(300)
    ]:
        corpo = t.encode("utf-16-le")
        _conferir(original, rapido, struct.pack("<H", len(corpo)) + corpo)
    assert rapido.read(_Reader(struct.pack("<H", 0xFFFF))) is None


def test_varchar_with_the_column_codec():
    cp = codecs.lookup("cp1252")
    original, rapido = _par(VarChar72Serializer, size=8000, codec=cp)
    for t in (
        TEXTOS[:-1]
        + ["\u20ac \u2018quotes\u2019 \u2013 dash"]
        + [
            "".join(chr(rnd.randrange(32, 256)) for _ in range(rnd.randrange(80)))
            for _ in range(300)
        ]
    ):
        try:
            corpo = t.encode("cp1252")
        except UnicodeEncodeError:
            continue
        _conferir(original, rapido, struct.pack("<h", len(corpo)) + corpo)
    assert rapido.read(_Reader(struct.pack("<h", -1))) is None


def test_varchar_sem_bytes_to_unicode_fica_como_esta():
    ser = VarChar72Serializer(size=10, codec=codecs.lookup("cp1252"))
    assert C.acelerar_leitura(_cursor([ser], unicode_=False)) == 0


def test_max_e_outros_tipos_nao_sao_trocados():
    outro = SimpleNamespace(read=lambda r: "x")
    maxs = NVarCharMaxSerializer(collation=raw_collation)
    assert C.acelerar_leitura(_cursor([outro, maxs])) == 0
    assert C.acelerar_leitura(SimpleNamespace()) == 0


def test_troca_e_da_instancia_nao_da_classe():
    a, b = MsDecimalSerializer(precision=18, scale=2), MsDecimalSerializer(precision=18, scale=2)
    C.acelerar_leitura(_cursor([a]))
    dados = _decimal_bytes(18, 12345, True)
    assert a.read(_Reader(dados)) == "123.45"
    assert type(b.read(_Reader(dados))).__name__ == "Decimal"


@pytest.mark.parametrize(
    "tamanho,formato,faixa",
    [
        (1, "<B", (0, 255)),
        (2, "<h", (-32768, 32767)),
        (4, "<l", (-(2**31), 2**31 - 1)),
        (8, "<q", (-(2**63), 2**63 - 1)),
    ],
)
def test_intn(tamanho, formato, faixa):
    original, rapido = _par(IntNSerializer, typ=IntNSerializer.type_by_size[tamanho])
    valores = list(faixa) + [0] + [rnd.randint(*faixa) for _ in range(500)]
    for v in valores:
        _conferir(original, rapido, bytes([tamanho]) + struct.pack(formato, v))
    assert rapido.read(_Reader(bytes([0]))) is None


def test_bitn():
    original, rapido = _par(BitNSerializer, typ=BitType())
    for dados in (bytes([1, 0]), bytes([1, 1]), bytes([1, 2])):
        _conferir(original, rapido, dados)
    assert rapido.read(_Reader(bytes([0]))) is None


def test_datetimen_8_e_smalldatetime_pelo_original():
    original, rapido = _par(DateTimeNSerializer, size=8)
    casos = [(0, 0), (-53690, 0), (2958463, 25919999), (46000, 299), (46000, 1), (46000, 2)]
    casos += [(rnd.randint(-53690, 2958463), rnd.randint(0, 25919999)) for _ in range(3000)]
    for dias, t in casos:
        _conferir(original, rapido, bytes([8]) + struct.pack("<ll", dias, t))
    assert rapido.read(_Reader(bytes([0]))) is None
    # SMALLDATETIME (4 bytes) goes through the original read; the text is still the one from texto().
    o4, r4 = _par(DateTimeNSerializer, size=4)
    _conferir(o4, r4, bytes([4]) + struct.pack("<HH", 45000, 600))


def test_datetimen_com_fuso_nao_e_trocado():
    ser = DateTimeNSerializer(8)
    assert C.acelerar_leitura(_cursor([ser], fuso=lambda _o: None)) == 0


def test_outra_versao_do_pytds_nao_troca_nada(monkeypatch):
    monkeypatch.setattr(C, "_PYTDS_OK", False)
    ser = MsDecimalSerializer(precision=18, scale=2)
    assert C.acelerar_leitura(_cursor([ser])) == 0


def test_versao_instalada_e_a_conhecida():
    from importlib.metadata import version

    assert version("python-tds") == C.PYTDS_CONHECIDO
