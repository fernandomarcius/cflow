"""`origem: "objeto"` source: ONE file of a read-only layer, addressed by path.

It exists for archives written by an external system into a bucket of their own, with a fixed
directory layout (for example `Archive_{year}/Sales/unit_{id}/Sales_{YYYY_MM_DD}.parquet`). There is
no pointer and no index: the file IS the address. For that reason this origin does not serve
silver/gold, which are always read through the pointer (`origem: "publicado"`), never by path,
because `execucao_id=` sits in the middle of the path.

- **The path is a TEMPLATE**, with `{parameter}` and `{parameter:<format>}`. The format is
  `strftime` over the parameter date (`{reference_month:%Y}`), and a leading `fim_mes:` moves it to
  the last day of the month (`{reference_month:fim_mes:%Y_%m_%d}` -> `2026_08_31`).
- **A value that is not a safe path segment is rejected**, never sanitised: `..` or `/` coming
  from a parameter would point at another bucket file, and the data would look right.
- **`opcional`**: a missing file becomes an EMPTY table with the declared `colunas` (the schema has
  nowhere else to come from, so declaring it is mandatory). Without `opcional`, absence is an error
  naming the object.
- **`modo: "existencia"`** only answers whether the file exists: one row (`objeto` = URI) or none.
  It separates "not written" from "written empty" - both give zero rows in `dados` mode.
- **The downloaded file is addressed by CONTENT** (sha256), not by path: the external writer
  REWRITES files at the same path, and a path-keyed cache would serve the stale version forever.
"""

from __future__ import annotations

import calendar
import hashlib
import os
import re
import uuid
from datetime import date
from pathlib import Path

from ..config import get_settings
from ..errors import ErroAplicacao
from .armazenamento import CAMADAS_SO_LEITURA, _validar_caminho, abrir
from .leitura import _valor, texto_particao

MODOS = ("dados", "existencia")
FORMATOS = ("parquet",)

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}")
_SEGURO = re.compile(r"^[A-Za-z0-9_.\-]+$")
_STRFTIME = re.compile(r"^(?:%[YmdHMS]|[A-Za-z0-9_\-.])+$")
_DIA_OU_MES = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")
_IDENT_COLUNA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parametros_do_caminho(caminho: str) -> list[str]:
    """The parameters the path template references, in order."""
    return [m.group(1) for m in _TOKEN.finditer(caminho or "")]


def problemas_do_modelo(caminho) -> list[str]:
    """What keeps the path template from becoming an address; empty when nothing does."""
    if not isinstance(caminho, str) or not caminho.strip():
        return ["config.caminho is required (the file to read, relative to the layer root)."]
    p: list[str] = []
    for m in _TOKEN.finditer(caminho):
        formato = m.group(2)
        if formato is None:
            continue
        if formato.startswith("fim_mes:"):
            formato = formato[len("fim_mes:") :]
        if not _STRFTIME.match(formato):
            p.append(
                f"config.caminho: format '{m.group(2)}' of '{{{m.group(1)}}}' is invalid (use "
                "%Y, %m, %d and literals; a leading 'fim_mes:' moves to the last day of the month)."
            )
    fixo = _TOKEN.sub("x", caminho)
    if "{" in fixo or "}" in fixo:
        p.append("config.caminho: unmatched '{' or '}' - use {parametro} or {parametro:formato}.")
    elif ".." in fixo.split("/") or fixo.startswith("/") or "\\" in fixo:
        p.append("config.caminho: path relative to the layer, without '..', '\\' or a leading '/'.")
    return p


def _data(nome: str, texto: str) -> date:
    m = _DIA_OU_MES.match(texto)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        pass
    raise ErroAplicacao(
        codigo="objeto_parametro_invalido",
        mensagem=f"Parameter '{nome}' ({texto!r}) is not a date: the path asks for a date format.",
        status=400,
    )


def montar_caminho(modelo: str, parametros: dict | None) -> str:
    """The template with parameters resolved. Loudly rejects anything that is not a valid path."""

    def _trocar(m: re.Match) -> str:
        nome, formato = m.group(1), m.group(2)
        bruto = _valor(f":{nome}", parametros)
        if bruto is None:
            raise ErroAplicacao(
                codigo="objeto_parametro_ausente",
                mensagem=f"The object path references '{{{nome}}}', but the parameter has no value.",
                status=400,
            )
        texto = texto_particao(bruto)
        if formato is not None:
            dia = _data(nome, texto)
            if formato.startswith("fim_mes:"):
                formato = formato[len("fim_mes:") :]
                dia = dia.replace(day=calendar.monthrange(dia.year, dia.month)[1])
            texto = dia.strftime(formato)
        if not _SEGURO.match(texto) or texto in (".", ".."):
            raise ErroAplicacao(
                codigo="objeto_parametro_invalido",
                mensagem=(
                    f"Parameter '{nome}' ({texto!r}) cannot be used in the object path "
                    "(only letters, digits, '_', '-' and '.')."
                ),
                status=400,
            )
        return texto

    return _validar_caminho(_TOKEN.sub(_trocar, modelo))


def _cache(camada: str, dados: bytes, formato: str) -> Path:
    """Write the content to the sha256-addressed cache (temp file + `os.replace`)."""
    digest = hashlib.sha256(dados).hexdigest()
    destino = Path(get_settings().lake_cache_dir) / camada / "_sha256" / f"{digest}.{formato}"
    if not destino.is_file():
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.parcial")
        try:
            tmp.write_bytes(dados)
            os.replace(tmp, destino)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return destino


async def resolver(cfg: dict, *, parametros: dict | None = None) -> dict:
    """`{camada, caminho, uri, existe, arquivo, sha256}`. `arquivo` is the LOCAL path DuckDB opens
    (None when the object does not exist and the source is `opcional`)."""
    camada = cfg.get("camada") or "data_locked"
    formato = cfg.get("formato") or "parquet"
    lake = await abrir(cfg.get("conexao_id"))
    caminho = montar_caminho(str(cfg.get("caminho") or ""), parametros)
    uri = lake.uri(camada, caminho)
    base = {"camada": camada, "caminho": caminho, "uri": uri}
    try:
        if lake.provedor == "local":
            # The object is already a local file: reading it in place always reads the current version.
            arquivo = await lake.materializar(camada, caminho, Path())
            return {**base, "existe": True, "arquivo": arquivo, "sha256": None}
        dados = await lake.ler(camada, caminho)
    except ErroAplicacao as e:
        if e.codigo != "objeto_ausente":
            raise
        if cfg.get("opcional"):
            return {**base, "existe": False, "arquivo": None, "sha256": None}
        raise ErroAplicacao(
            codigo="objeto_ausente",
            mensagem=f"The object {uri} does not exist in layer '{camada}'.",
            status=400,
        ) from None
    destino = _cache(camada, dados, formato)
    return {**base, "existe": True, "arquivo": str(destino), "sha256": destino.stem}


def _literal(texto: str) -> str:
    return "'" + str(texto).replace("'", "''") + "'"


def sql_relacao(lido: dict, cfg: dict) -> str:
    """The SELECT that materialises the source: the file, the existence marker or the empty table."""
    if (cfg.get("modo") or "dados") == "existencia":
        if lido["existe"]:
            return f'SELECT {_literal(lido["uri"])} AS "objeto"'
        return 'SELECT CAST(NULL AS VARCHAR) AS "objeto" WHERE false'
    if lido["existe"]:
        return f"SELECT * FROM read_parquet({_literal(lido['arquivo'])})"
    colunas = ", ".join(f'CAST(NULL AS VARCHAR) AS "{c}"' for c in cfg.get("colunas") or [])
    return f"SELECT {colunas} WHERE false"


def problemas_da_config(cfg: dict) -> list[str]:
    """Structural validation of the `objeto` source (called by `definicao._validar_fonte`)."""
    p: list[str] = []
    if not cfg.get("conexao_id"):
        p.append("config.conexao_id is required (the object connection that holds the layer).")
    camada = cfg.get("camada") or "data_locked"
    if camada not in CAMADAS_SO_LEITURA:
        p.append(
            f"config.camada accepts {', '.join(CAMADAS_SO_LEITURA)} - silver and gold are read "
            "through the pointer (origin 'publicado'), never by path."
        )
    if (cfg.get("formato") or "parquet") not in FORMATOS:
        p.append(f"config.formato accepts {', '.join(FORMATOS)}.")
    modo = cfg.get("modo") or "dados"
    if modo not in MODOS:
        p.append(f"config.modo accepts {', '.join(MODOS)}.")
    if "opcional" in cfg and not isinstance(cfg["opcional"], bool):
        p.append("config.opcional must be true or false.")
    colunas = cfg.get("colunas")
    if colunas is not None and (
        not isinstance(colunas, list)
        or not colunas
        or not all(isinstance(c, str) and _IDENT_COLUNA.match(c) for c in colunas)
    ):
        p.append("config.colunas must be a list of column names (plain identifiers).")
    if cfg.get("opcional") is True and modo == "dados" and not colunas:
        p.append(
            "config.opcional in 'dados' mode requires config.colunas: with the file missing, the "
            "schema of the empty table has nowhere to come from."
        )
    for campo in ("pipeline_id", "fonte_id", "dataset", "particao"):
        if cfg.get(campo):
            p.append(
                f"config.{campo} does not apply to the 'objeto' origin (the address is config.caminho)."
            )
    return p + problemas_do_modelo(cfg.get("caminho"))
