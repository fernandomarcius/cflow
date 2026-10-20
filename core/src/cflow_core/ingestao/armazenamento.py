"""Lake object storage backends: `local` (default) and `gcs`.

Why an abstraction and not the SDK directly: the platform must WORK air-gapped. With a pluggable
backend, switching the cloud off is a configuration change - the engine, the tests and the
air-gapped mode all run against disk, and GCS is just one more byte destination.

The credential (service-account JSON) comes from the **secret store**, resolved at runtime; never
from `.env`, code, logs or audit diffs. The `gcs` backend speaks the JSON API through `httpx` - no
new dependency, and no second credential path outside the secret store.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ..config import get_settings
from ..errors import ErroAplicacao
from ..io_lake import em_thread_lake
from ..retry import RETENTAVEIS, espera
from ..voo_unico import VooUnico

logger = logging.getLogger("cflow.ingestao.armazenamento")

#: A cache file idle for less than this is not deleted by the trimmer: between `materializar`
#: returning the path and DuckDB opening it there is a window, and deleting in it would make the
#: read fail. Once opened, deleting does not matter (the inode lives until the last descriptor
#: closes). Under heavy load a longer window made the cap ineffective: at ~17 MB/s from GCS, ten
#: minutes of downloads are ~10 GB, all "recent". With 120 s the excess over the cap stays at
#: roughly 2 GB in the worst case.
IDADE_MINIMA_APARO_S = 120

#: One trim at a time in this process: two downloads finishing together would scan the same total
#: and each delete the whole excess - twice what is needed, and the extra deleted dataset is
#: downloaded again on the next read.
_TRAVA_APARO = threading.Lock()


def _tocar(caminho: Path) -> None:
    try:
        os.utime(caminho)
    except OSError:
        pass  # vanished between the `is_file` check and here: the next access downloads it again


def aparar_cache(
    raiz: Path,
    teto_bytes: int,
    *,
    proteger: Path | None = None,
    idade_minima_s: float = IDADE_MINIMA_APARO_S,
) -> int:
    """Delete the oldest-`mtime` files from the lake cache until the total fits in `teto_bytes`.
    Returns the bytes deleted.

    The cache is DERIVED (content-addressed path, immutable layer): whatever leaves comes back on
    the next `materializar`. Not eligible: in-progress download temp files (names starting with
    `.`), the `proteger` file (the one just downloaded), files used less than `idade_minima_s`
    ago, and files with more than one hardlink (deleting this name does not free disk). The total
    compared with the cap is what the CACHE occupies: it counts the in-progress temp file and does
    not count snapshot hardlinks. If only those remain, the total may stay above the cap - better
    than deleting what is in use. O(cache files), once per new download."""
    with _TRAVA_APARO:
        return _aparar_cache(raiz, teto_bytes, proteger=proteger, idade_minima_s=idade_minima_s)


def _aparar_cache(
    raiz: Path, teto_bytes: int, *, proteger: Path | None, idade_minima_s: float
) -> int:
    agora = time.time()
    itens = []
    total = 0
    linkados = 0
    for pasta, _, nomes in os.walk(raiz):
        for nome in nomes:
            caminho = Path(pasta) / nome
            try:
                st = caminho.stat()
            except OSError:
                continue
            # What the file OCCUPIES, not its logical size.
            ocupado = getattr(st, "st_blocks", 0) * 512 or st.st_size
            if st.st_nlink > 1:
                # Hardlink of a handoff snapshot (`engine._copiar_atomico`): the space belongs to
                # the snapshot, not the cache - deleting this name does not return it. Kept out of
                # the total and the candidates; otherwise, with the linked files above the cap,
                # every download would pointlessly empty the rest of the cache. Logged separately.
                linkados += ocupado
                continue
            total += ocupado
            if nome.startswith("."):
                continue  # download in progress: occupies disk (counts in the total) but is never deleted
            itens.append((st.st_mtime, ocupado, caminho))
    if total <= teto_bytes:
        return 0
    protegido = proteger.resolve() if proteger is not None else None
    apagados = 0
    for mtime, tamanho, caminho in sorted(itens, key=lambda i: i[0]):
        if total <= teto_bytes:
            break
        if agora - mtime < idade_minima_s or (
            protegido is not None and caminho.resolve() == protegido
        ):
            continue
        try:
            caminho.unlink()
        except OSError:
            continue
        total -= tamanho
        apagados += tamanho
    if apagados:
        logger.info(
            "lake_cache_aparado",
            extra={
                "bytes_apagados": apagados,
                "bytes_restantes": total,
                "bytes_linkados": linkados,
                "teto": teto_bytes,
            },
        )
    return apagados


#: One download per cache file at a time in this process (`ArmazenamentoGcs.materializar`).
VOO_DOWNLOAD = VooUnico()
#: Chunk size for GCS downloads into the cache (`materializar`): the object is never fully in memory.
BLOCO_DOWNLOAD = 1024 * 1024

# `gold` is CONFORMED business product (joins several origins, applies rules, creates columns that
# exist nowhere else). It is not "improved silver": silver is 1:1 with the bronze table, only
# typed. Mixing both in the same bucket would make the silver consumer find datasets that follow
# none of its rules - and the gold consumer find a raw table dressed up as a product.
# `relatorios` holds the export ARTIFACT (xlsx) addressed by the tuple (run, node, layout,
# version, format): same click, same bytes. It lives in the lake because it is the only volume api
# and worker already share - and it is immutable for the same reason as landing: the file is
# evidence of what was delivered to someone.
# `data_locked` is a read-only archive written by an external system into a bucket of its own.
# The platform only READS it - through the `origem: "objeto"` source -, never writes: it is the
# evidence of what the other system delivered, and it belongs to someone else.
CAMADAS = ("landing", "bronze", "quarantine", "silver", "gold", "relatorios", "data_locked")
# Layers the platform never writes, even when asked: writing there would rewrite the history of
# another system. The refusal belongs to the layer, like the immutability below.
CAMADAS_SO_LEITURA = ("data_locked",)
# Immutability is a property of the LAYER, not of the `gravar` method. Landing, bronze and
# quarantine hold EVIDENCE - overwriting erases what they exist to prove. Silver is derived and
# rewritten on every run, but even there the rewrite is of the POINTER, not the data (see
# `ingestao.leitura.publicar_dataset`): the Parquet goes to a per-run path and the only mutable
# object in the whole lake is a pointer JSON. Gold follows the SAME discipline.
CAMADAS_IMUTAVEIS = ("landing", "bronze", "quarantine", "relatorios")
_ESCOPO_GCS = "https://www.googleapis.com/auth/devstorage.read_write"
# KMS does NOT accept the storage scope: `MacSign` answers 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT` to
# a `devstorage.read_write` token. That is why PII tokenisation asks for its OWN token, and the
# minimum Google publishes for KMS is `cloud-platform` - there is no scope restricted to `cloudkms`.
# The effective privilege remains that of the key IAM policy (signer/verifier role granted only to
# the ingestion identity), which is where the real restriction lives.
_ESCOPO_KMS = "https://www.googleapis.com/auth/cloud-platform"


_ACAO_EN = {"ler": "read", "gravar": "write"}


def _codigo_falha(acao: str) -> str:
    """The lake error codes did NOT change with retries - only the number of attempts before them.
    Whoever handles `falha_ler_lake`/`falha_gravacao_lake` keeps handling the same thing."""
    return "falha_ler_lake" if acao == "ler" else "falha_gravacao_lake"


def _causa_transporte(exc: Exception) -> str:
    """Sanitised cause of a network failure. It goes to the audit trail and the UI: a URL with a
    query token inside must not leak through there."""
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "host unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return "network failure"


def _validar_camada(camada: str) -> None:
    if camada not in CAMADAS:
        raise ErroAplicacao(
            codigo="camada_invalida",
            mensagem=f"Invalid lake layer: '{camada}' (use {', '.join(CAMADAS)}).",
            status=400,
        )


def _validar_sobrescrita(camada: str, sobrescrever: bool) -> None:
    """Refuse overwriting in the evidence layers even when the caller asks explicitly - and ANY
    write to the read-only layers (this is the point every write passes through)."""
    if camada in CAMADAS_SO_LEITURA:
        raise ErroAplicacao(
            codigo="camada_so_leitura",
            mensagem=f"Layer '{camada}' is read-only: the platform does not write to it.",
            status=409,
        )
    if sobrescrever and camada in CAMADAS_IMUTAVEIS:
        raise ErroAplicacao(
            codigo="camada_imutavel",
            mensagem=f"Layer '{camada}' is immutable: overwriting would erase the evidence.",
            status=409,
        )


def _validar_caminho(caminho: str) -> str:
    """Relative path that cannot escape the layer. `..` in a file name coming from an external
    portal would write outside the bucket/directory - the classic traversal, and the file name
    here is third-party data."""
    c = str(caminho).strip().lstrip("/")
    if not c or ".." in c.split("/") or c.startswith("/") or "\\" in c:
        raise ErroAplicacao(
            codigo="caminho_invalido",
            mensagem="Invalid object path.",
            status=400,
        )
    return c


class Armazenamento(Protocol):
    """Contract of the lake byte destination. `gravar` is IMMUTABLE by default: overwriting
    landing erases the evidence it exists to keep."""

    provedor: str

    def uri(self, camada: str, caminho: str) -> str: ...

    def raiz_uri(self, camada: str) -> str:
        """Prefix of this layer's URIs. Used to answer "is this batch in THIS lake?" - a question
        incremental loading must ask before trusting an old ETag."""
        ...

    async def gravar(
        self,
        camada: str,
        caminho: str,
        dados: bytes,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str: ...

    async def gravar_arquivo(
        self,
        camada: str,
        caminho: str,
        origem: Path,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str:
        """Like `gravar`, but from a FILE already on disk.

        It exists because publishing a dataset used to read the WHOLE Parquet into memory only to
        write it again - and partitioned publishing does that for several scopes at once. The local
        backend uses `os.link`, copying no byte at all; the others fall back to the default
        implementation below, which is exactly the previous behaviour."""
        return await self.gravar(
            camada,
            caminho,
            await em_thread_lake(origem.read_bytes),
            tipo_mime=tipo_mime,
            sobrescrever=sobrescrever,
        )

    async def ler(self, camada: str, caminho: str) -> bytes: ...

    async def existe(self, camada: str, caminho: str) -> bool: ...

    async def copiar(self, camada: str, origem: str, destino: str) -> dict:
        """Copy within the layer without overwriting, and prove byte equivalence.

        Returns size/checksum and whether an equivalent destination already existed. A divergent
        destination is a hard conflict, never an idempotent no-op.
        """
        ...

    async def materializar(self, camada: str, caminho: str, destino: Path) -> str:
        """Deliver the object as a LOCAL FILE and return the path, for DuckDB to open.

        Local backend: returns the object itself, without copying - air-gapped mode pays nothing.
        Remote: downloads THROUGH THE BACKEND (never `httpfs`/`gs://`, which would be a second
        credential door outside the secret store)."""
        ...

    async def materializar_confiavel(self, camada: str, caminho: str, destino: Path) -> str:
        """Materialise without returning an internal path that could be swapped for a symlink.

        Object backends already download into ``destino``. The local one copies from an open
        descriptor with ``O_NOFOLLOW``; use it in security maintenance that validates bytes before
        writing.
        """
        ...

    async def materializar_backup_confiavel(self, backup: str, caminho: str, destino: Path) -> str:
        """Materialise a backup object that is a sibling of the layers.

        Deliberately available only on the local backend. Physical backups have no implicit
        equivalent in remote storage; guessing a bucket/prefix would read from another trust root.
        """
        ...

    async def testar(self) -> dict: ...


# --- Local (default; air-gapped, tests, dev) ---------------------------------------------------


def _conferir_caixa(raiz_camada: Path, destino: Path) -> None:
    """Refuse writing into a hive segment that already exists on disk with a DIFFERENT case.

    Backend parity, and not an academic one: on GCS names are case-sensitive, on APFS/HFS+ (the
    macOS default, and air-gapped mode runs on disk) they are not. Without this check, publishing
    `project=acme` after `project=ACME` would silently reuse the other directory - whoever asked
    for the `acme` dataset would receive the data of `ACME`, with no error, and the same
    definition would give different results depending on where the lake lived. Mixed-case codes
    are the normal case.

    Only looks at hive segments (`key=value`): the only ones that carry outside data - the dataset
    is a validated slug and the run uuid is born lowercase."""
    atual = raiz_camada
    for segmento in destino.parent.relative_to(raiz_camada).parts:
        proximo = atual / segmento
        # `exists()` does not work here: it is the one that is case-insensitive on the filesystem in
        # question. The question is whether the LITERAL name is in the listing - hence `iterdir`,
        # not a `stat`. `execucao_id=` is left out: it is the segment that grows with every run (one
        # entry per run in the scope directory), so listing it cost O(history) per write - and the
        # uuid is born lowercase, so no case collision is possible.
        if "=" in segmento and not segmento.startswith("execucao_id=") and atual.is_dir():
            nomes = {p.name for p in atual.iterdir()}
            if segmento not in nomes:
                colide = [n for n in nomes if n.casefold() == segmento.casefold()]
                if colide:
                    raise ErroAplicacao(
                        codigo="particao_caixa_conflitante",
                        mensagem=(
                            f"Scope '{segmento}' differs from '{colide[0]}' only in case, and "
                            "this lake does not tell the two apart: one publication would end up "
                            "describing the other. Standardise the value case at the source."
                        ),
                        status=409,
                    )
        atual = proximo


def _recusar_symlink_local(raiz_camada: Path, destino: Path) -> None:
    """Refuse symbolic components under the trusted root of the local backend.

    `_validar_caminho` blocks `..`, but an existing intermediate directory could still be a
    symlink pointing outside the lake. Reading would follow the link; writing would create the file
    outside the root. The configured root is the trust anchor (it may itself be a mounted volume);
    below it no symlink takes part in an object path.
    """
    try:
        partes = destino.relative_to(raiz_camada).parts
    except ValueError as exc:
        raise ErroAplicacao(
            codigo="caminho_inseguro",
            mensagem="Local path escapes the trusted lake root.",
            status=409,
        ) from exc
    atual = raiz_camada
    for parte in partes:
        atual /= parte
        if atual.is_symlink():
            raise ErroAplicacao(
                codigo="caminho_inseguro",
                mensagem="Local path contains a symbolic link under the lake root.",
                status=409,
            )


@dataclass
class ArmazenamentoLocal:
    raiz: Path
    provedor: str = "local"

    def _caminho(self, camada: str, caminho: str) -> Path:
        _validar_camada(camada)
        raiz_camada = self.raiz / camada
        destino = raiz_camada / _validar_caminho(caminho)
        _recusar_symlink_local(raiz_camada, destino)
        return destino

    def _abrir_pasta_objeto(self, camada: str, caminho: str, *, criar: bool) -> tuple[int, str]:
        """Open the parent through anchored descriptors; no component follows a symlink.

        The descriptor keeps pointing at the validated directory even if another process later
        swaps its name for a symlink. Operations on the final name use ``dir_fd`` and close the
        TOCTOU window that validation through ``Path.resolve``/``is_symlink`` never closes.
        """
        _validar_camada(camada)
        partes = Path(_validar_caminho(caminho)).parts
        if not partes:
            raise ErroAplicacao(
                codigo="caminho_invalido", mensagem="Invalid object path.", status=400
            )
        flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if criar:
            self.raiz.mkdir(parents=True, exist_ok=True)
        try:
            atual = os.open(self.raiz, flags_dir)
        except FileNotFoundError:
            raise
        try:
            for parte in (camada, *partes[:-1]):
                try:
                    proximo = os.open(parte, flags_dir, dir_fd=atual)
                except FileNotFoundError:
                    if not criar:
                        raise
                    try:
                        os.mkdir(parte, dir_fd=atual)
                    except FileExistsError:
                        pass
                    proximo = os.open(parte, flags_dir, dir_fd=atual)
                except OSError as exc:
                    raise ErroAplicacao(
                        codigo="caminho_inseguro",
                        mensagem="Local path contains a symbolic link under the lake root.",
                        status=409,
                    ) from exc
                os.close(atual)
                atual = proximo
            return atual, partes[-1]
        except BaseException:
            os.close(atual)
            raise

    def _abrir_objeto_leitura(self, camada: str, caminho: str) -> int:
        try:
            pasta_fd, nome = self._abrir_pasta_objeto(camada, caminho, criar=False)
        except FileNotFoundError:
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem="Object not found in the lake.", status=404
            ) from None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                arquivo_fd = os.open(nome, flags, dir_fd=pasta_fd)
            except FileNotFoundError:
                raise ErroAplicacao(
                    codigo="objeto_ausente",
                    mensagem="Object not found in the lake.",
                    status=404,
                ) from None
            except OSError as exc:
                raise ErroAplicacao(
                    codigo="caminho_inseguro",
                    mensagem="Local object is a symbolic link.",
                    status=409,
                ) from exc
            return arquivo_fd
        finally:
            os.close(pasta_fd)

    def _abrir_objeto_backup_leitura(self, backup: str, caminho: str) -> int:
        """Open a backup sibling of ``gold`` without accepting relative paths or symlinks.

        ``self.raiz`` remains the trust anchor. Each segment is opened by descriptor with
        ``O_NOFOLLOW``; neither a concurrent directory swap nor a pre-existing symlink escapes it.
        The domain caller decides which backup name is allowed.
        """
        partes_backup = backup.split("/")
        partes_caminho = caminho.split("/")
        if (
            len(partes_backup) != 1
            or any(parte in {"", ".", ".."} for parte in partes_backup)
            or any(parte in {"", ".", ".."} for parte in partes_caminho)
            or backup != backup.strip()
            or caminho != caminho.strip()
            or backup.startswith("/")
            or caminho.startswith("/")
            or "\\" in backup
            or "\\" in caminho
        ):
            raise ErroAplicacao(
                codigo="caminho_invalido",
                mensagem="Invalid backup path.",
                status=400,
            )
        flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags_arquivo = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            atual = os.open(self.raiz, flags_dir)
        except FileNotFoundError:
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem="Object not found in the backup.", status=404
            ) from None
        try:
            for parte in (backup, *partes_caminho[:-1]):
                try:
                    proximo = os.open(parte, flags_dir, dir_fd=atual)
                except FileNotFoundError:
                    raise ErroAplicacao(
                        codigo="objeto_ausente",
                        mensagem="Object not found in the backup.",
                        status=404,
                    ) from None
                except OSError as exc:
                    raise ErroAplicacao(
                        codigo="caminho_inseguro",
                        mensagem="Backup path contains a symbolic link.",
                        status=409,
                    ) from exc
                os.close(atual)
                atual = proximo
            try:
                return os.open(partes_caminho[-1], flags_arquivo, dir_fd=atual)
            except FileNotFoundError:
                raise ErroAplicacao(
                    codigo="objeto_ausente",
                    mensagem="Object not found in the backup.",
                    status=404,
                ) from None
            except OSError as exc:
                raise ErroAplicacao(
                    codigo="caminho_inseguro",
                    mensagem="Backup object is a symbolic link.",
                    status=409,
                ) from exc
        finally:
            os.close(atual)

    def uri(self, camada: str, caminho: str) -> str:
        return f"file://{self._caminho(camada, caminho)}"

    def raiz_uri(self, camada: str) -> str:
        _validar_camada(camada)
        return f"file://{self.raiz / camada}/"

    async def gravar(
        self,
        camada: str,
        caminho: str,
        dados: bytes,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str:
        _validar_sobrescrita(camada, sobrescrever)
        destino = self._caminho(camada, caminho)

        def _escrever() -> None:
            _conferir_caixa(self.raiz / camada, destino)
            pasta_fd, nome = self._abrir_pasta_objeto(camada, caminho, criar=True)
            tmp = f".{nome}.{uuid.uuid4().hex}.parcial"
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                temporario_fd = os.open(tmp, flags, 0o600, dir_fd=pasta_fd)
                with os.fdopen(temporario_fd, "wb") as fp:
                    fp.write(dados)
                if sobrescrever:
                    os.replace(tmp, nome, src_dir_fd=pasta_fd, dst_dir_fd=pasta_fd)
                else:
                    # Publish the COMPLETE inode in one syscall that fails if the name exists.
                    # An O_EXCL placeholder + replace left an empty final file if the process died
                    # between the two operations, blocking every immutable rerun.
                    os.link(
                        tmp,
                        nome,
                        src_dir_fd=pasta_fd,
                        dst_dir_fd=pasta_fd,
                        follow_symlinks=False,
                    )
                    os.unlink(tmp, dir_fd=pasta_fd)
            except FileExistsError:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=pasta_fd)
                raise
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp, dir_fd=pasta_fd)
                raise
            finally:
                os.close(pasta_fd)

        try:
            await em_thread_lake(_escrever)
        except FileExistsError:
            raise ErroAplicacao(
                codigo="objeto_ja_existe",
                mensagem="Object already exists in the lake (the layer is immutable).",
                status=409,
            ) from None
        return self.uri(camada, caminho)

    async def gravar_arquivo(
        self,
        camada: str,
        caminho: str,
        origem: Path,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str:
        """Publish by LINK: the lake object becomes the same inode as the source file.

        Safe because both sides are immutable - the node snapshot is written to a temp file and
        swapped with `os.replace` (never in place), and the lake layer refuses overwriting. If
        linking is not possible (source and lake on different filesystems, as with a separately
        mounted volume), it copies - and the cost is then the old one, without going through the
        process memory."""
        _validar_sobrescrita(camada, sobrescrever)
        destino = self._caminho(camada, caminho)

        def _ligar() -> None:
            _conferir_caixa(self.raiz / camada, destino)
            destino.parent.mkdir(parents=True, exist_ok=True)
            tmp = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.parcial")
            try:
                try:
                    os.link(origem, tmp)
                except OSError:  # another filesystem, or the FS has no hardlinks
                    shutil.copyfile(origem, tmp)
                if sobrescrever:
                    os.replace(tmp, destino)
                else:
                    os.link(tmp, destino)
                    tmp.unlink()
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

        try:
            await em_thread_lake(_ligar)
        except FileExistsError:
            raise ErroAplicacao(
                codigo="objeto_ja_existe",
                mensagem="Object already exists in the lake (the layer is immutable).",
                status=409,
            ) from None
        return self.uri(camada, caminho)

    async def ler(self, camada: str, caminho: str) -> bytes:
        def _ler() -> bytes:
            arquivo_fd = self._abrir_objeto_leitura(camada, caminho)
            with os.fdopen(arquivo_fd, "rb") as arquivo:
                return arquivo.read()

        return await em_thread_lake(_ler)

    async def existe(self, camada: str, caminho: str) -> bool:
        def _existe() -> bool:
            try:
                arquivo_fd = self._abrir_objeto_leitura(camada, caminho)
            except ErroAplicacao as exc:
                if exc.codigo == "objeto_ausente":
                    return False
                raise
            os.close(arquivo_fd)
            return True

        return await em_thread_lake(_existe)

    async def copiar(self, camada: str, origem: str, destino: str) -> dict:
        src, dst = self._caminho(camada, origem), self._caminho(camada, destino)

        def _sha256(p: Path) -> str:
            h = hashlib.sha256()
            with p.open("rb") as fp:
                for bloco in iter(lambda: fp.read(1024 * 1024), b""):
                    h.update(bloco)
            return h.hexdigest()

        def _resultado_se_equivalente(tamanho: int, sha: str) -> dict | None:
            if not dst.is_file():
                return None
            if dst.stat().st_size != tamanho or _sha256(dst) != sha:
                return None
            return {"bytes": tamanho, "sha256": sha, "ja_existia": True}

        def _copiar() -> dict:
            if not src.is_file():
                raise ErroAplicacao(
                    codigo="objeto_ausente", mensagem="Object not found in the lake.", status=404
                )
            tamanho = src.stat().st_size
            sha = _sha256(src)
            if dst.exists():
                resultado = _resultado_se_equivalente(tamanho, sha)
                if resultado is None:
                    raise ErroAplicacao(
                        codigo="copia_lake_conflitante",
                        mensagem="The copy destination already exists with different bytes.",
                        status=409,
                    )
                return resultado
            _conferir_caixa(self.raiz / camada, dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                # `link` publishes the final name in one step: before the syscall it does not
                # exist; after, it already references the complete inode of the source. Creating
                # the destination with O_EXCL and only then calling `replace` left an observable
                # window with a zero-byte file. Source and destination belong to the same local
                # lake, so they are on the same FS.
                os.link(src, dst)
            except FileExistsError:
                # Another worker won the race between `exists` and `link`. The race is only a
                # no-op if the bytes it published are provably the same.
                resultado = _resultado_se_equivalente(tamanho, sha)
                if resultado is not None:
                    return resultado
                raise ErroAplicacao(
                    codigo="copia_lake_conflitante",
                    mensagem="The copy destination already exists with different bytes.",
                    status=409,
                ) from None
            if dst.stat().st_size != tamanho or _sha256(dst) != sha:
                raise ErroAplicacao(
                    codigo="falha_gravacao_lake",
                    mensagem="The copy in the lake finished with divergent bytes.",
                    status=502,
                )
            return {"bytes": tamanho, "sha256": sha, "ja_existia": False}

        return await em_thread_lake(_copiar)

    async def materializar(self, camada: str, caminho: str, destino: Path) -> str:
        """Zero copy: the object is ALREADY a local file. `destino` is ignored on purpose - copying
        to a cache would duplicate the whole lake on disk for no gain."""
        origem = self._caminho(camada, caminho)
        if not await em_thread_lake(origem.is_file):
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem="Object not found in the lake.", status=404
            )
        return str(origem)

    async def materializar_confiavel(self, camada: str, caminho: str, destino: Path) -> str:
        """Copy to a cache from an anchored descriptor; safe against symlink swaps."""

        def _copiar() -> str:
            arquivo_fd = self._abrir_objeto_leitura(camada, caminho)
            destino.parent.mkdir(parents=True, exist_ok=True)
            tmp = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.parcial")
            try:
                origem = os.fdopen(arquivo_fd, "rb")
                arquivo_fd = -1
                with origem, tmp.open("wb") as saida:
                    shutil.copyfileobj(origem, saida, length=1024 * 1024)
                os.replace(tmp, destino)
            except BaseException:
                if arquivo_fd >= 0:
                    os.close(arquivo_fd)
                tmp.unlink(missing_ok=True)
                raise
            return str(destino)

        return await em_thread_lake(_copiar)

    async def materializar_backup_confiavel(self, backup: str, caminho: str, destino: Path) -> str:
        """Copy a layer-sibling backup from a descriptor anchored at the lake root."""

        def _copiar() -> str:
            arquivo_fd = self._abrir_objeto_backup_leitura(backup, caminho)
            destino.parent.mkdir(parents=True, exist_ok=True)
            tmp = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.parcial")
            try:
                origem = os.fdopen(arquivo_fd, "rb")
                arquivo_fd = -1
                with origem, tmp.open("wb") as saida:
                    shutil.copyfileobj(origem, saida, length=1024 * 1024)
                os.replace(tmp, destino)
            except BaseException:
                if arquivo_fd >= 0:
                    os.close(arquivo_fd)
                tmp.unlink(missing_ok=True)
                raise
            return str(destino)

        return await em_thread_lake(_copiar)

    async def testar(self) -> dict:
        def _checar() -> dict:
            self.raiz.mkdir(parents=True, exist_ok=True)
            if not os.access(self.raiz, os.W_OK):
                return {"ok": False, "causa": "lake directory is not writable"}
            return {"ok": True, "provedor": "local", "raiz": str(self.raiz)}

        return await em_thread_lake(_checar)


# --- GCS (JSON API, credential from the secret store) ------------------------------------------


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _md5_b64(dados: bytes) -> str:
    """md5 in base64 - the format GCS speaks in both directions (`x-goog-hash` and `md5Hash`).

    md5 here is not a security choice (the object is immutable and authentication is the Bearer
    token): it detects corruption in transit, and it is the only digest the JSON API returns for
    free in the upload body. `usedforsecurity=False` tells the runtime so - and keeps the
    dependency count at ZERO. `google-crc32c` (a wheel with a C extension) would give marginally
    better detection at a supply-chain cost that does not pay off; it is recorded as a conscious
    option, and left out."""
    return base64.b64encode(hashlib.md5(dados, usedforsecurity=False).digest()).decode()


def _md5_do_cabecalho(valor: str | None) -> str | None:
    """`x-goog-hash: crc32c=AAAAAA==,md5=1B2M2Y8A...` -> the md5 piece, or `None`.

    `None` is a legitimate answer: not every response carries the header, and one carrying only
    `crc32c` gives nothing to check. Inventing a failure for a missing header would turn a good
    read into an infrastructure error."""
    for parte in (valor or "").split(","):
        parte = parte.strip()
        if parte.startswith("md5="):
            return parte[4:].strip() or None
    return None


# Access-token cache PER IDENTITY, and at MODULE level - not per instance. The reason is measured:
# `abrir()` builds a NEW instance on every call and ingestion calls it ONCE PER SLICE
# (`service._lake_da_fonte`). With the cache in a dataclass field it started empty on every slice,
# and a ten-year monthly backfill performed 120 RS256 signatures + 120 jwt-bearer exchanges -
# exactly the scenario the `_acesso` docstring already described as "makes a backfill hit 429".
#
# The key is a HASH of `client_email` + `token_uri`. Never private-key material: dictionary keys
# show up in tracebacks, debugging `repr`s and memory dumps. Two credentials of the same service
# account share the token on purpose - it is the same identity and the same scope; what differs
# between them (the private key) only serves to OBTAIN the token.
#
# No lock: two concurrent slices may, at start-up, trigger two exchanges. That wastes one request,
# it is not an error - and it is still three orders of magnitude below what existed before.
_TOKENS: dict[str, tuple[str, float]] = {}


def limpar_cache_token() -> None:
    """Empty the process token cache. Serves credential rotation in operation and tests, which
    must count token exchanges and cannot inherit another test's token."""
    _TOKENS.clear()


#: GCS HTTP clients per (event loop, factory, timeout). Each request used to open and close its own
#: `httpx.AsyncClient`: without keep-alive, every object paid TCP + TLS again - a partitioned
#: publication chains ~6 requests (data, manifest, scope pointer, immutable index, read and write
#: of the current index), ~18 round trips to the bucket. With the client reused, the connection
#: stays open between them. Per loop, because the client binds to the loop it was born in (the
#: worker has one; each test has its own); and the FACTORY is part of the key so that a replaced
#: `httpx.AsyncClient` (the tests' simulated transport) never receives another one's client.
_CLIENTES_HTTP: dict[tuple, object] = {}


def _cliente_http(timeout: int):
    import httpx

    loop = asyncio.get_running_loop()
    chave = (id(loop), id(httpx.AsyncClient), timeout)
    entrada = _CLIENTES_HTTP.get(chave)
    # The `id` of a CLOSED loop may be reused by a new one: the entry stores the loop itself and is
    # only valid if it is this one, alive. Entries of closed loops leave the dictionary when the
    # next one is created.
    if entrada is not None and entrada[0] is loop and not entrada[1].is_closed:
        return entrada[1]
    for k in [k for k, (lp, _) in _CLIENTES_HTTP.items() if lp.is_closed()]:
        _CLIENTES_HTTP.pop(k, None)
    # Keep-alive for the connections the worker actually keeps in flight. The httpx default keeps
    # only 20 idle: with 8 activities publishing gold (up to 8 scopes each), the excess close after
    # use and the next round trip pays TCP + TLS again (~100 ms to GCS from a cluster node).
    cli = httpx.AsyncClient(
        timeout=timeout,
        # `keepalive_expiry` of 60 s (the default is 5): during a run, publications come in bursts,
        # and a connection idle for more than 5 s was closed - measured against the bucket, a read
        # after 6.5 s idle cost 307 ms with 5 s and 230 ms with 120 s (the handshake redone).
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=64, keepalive_expiry=60),
    )
    _CLIENTES_HTTP[chave] = (loop, cli)
    return cli


#: Above this, the upload md5 is computed on the lake thread (see `ArmazenamentoGcs.gravar`) and
#: `gravar_arquivo` streams, instead of reading the whole file into memory.
_MD5_NO_LOOP_ATE = 1 << 20

#: Streaming upload block (and the md5 pass over the file).
_BLOCO_UPLOAD = 1 << 20


def _md5_e_tamanho_do_arquivo(origem: Path) -> tuple[str, int]:
    """md5 (base64, GCS format) and size of the file, in one block-by-block pass."""
    h = hashlib.md5(usedforsecurity=False)
    n = 0
    with origem.open("rb") as f:
        while bloco := f.read(_BLOCO_UPLOAD):
            h.update(bloco)
            n += len(bloco)
    return base64.b64encode(h.digest()).decode(), n


def _corpo_do_arquivo(origem: Path):
    """Streaming body factory: each call opens the file again (one per attempt)."""

    async def _blocos():
        f = await em_thread_lake(origem.open, "rb")
        try:
            while bloco := await em_thread_lake(f.read, _BLOCO_UPLOAD):
                yield bloco
        finally:
            await em_thread_lake(f.close)

    return _blocos


@dataclass
class ArmazenamentoGcs:
    """Google Cloud Storage JSON API. The bucket CMEK applies by default on the server side;
    `chave_kms` in the configuration forces a specific key per object."""

    buckets: dict
    credencial: dict
    prefixo: str = ""
    chave_kms: str | None = None
    timeout: int = 120
    # `conta_servico` (JSON in the secret store) or `token` (access token in the secret store - for
    # organisations that block key creation).
    autenticacao: str = "conta_servico"
    provedor: str = "gcs"

    def _bucket(self, camada: str) -> str:
        _validar_camada(camada)
        b = (self.buckets or {}).get(camada)
        if not b:
            raise ErroAplicacao(
                codigo="bucket_nao_configurado",
                mensagem=f"The lake connection does not define the bucket of layer '{camada}'.",
                status=400,
            )
        return b

    def _objeto(self, caminho: str) -> str:
        c = _validar_caminho(caminho)
        return f"{self.prefixo.strip('/')}/{c}" if self.prefixo else c

    def uri(self, camada: str, caminho: str) -> str:
        return f"gs://{self._bucket(camada)}/{self._objeto(caminho)}"

    def raiz_uri(self, camada: str) -> str:
        prefixo = f"{self.prefixo.strip('/')}/" if self.prefixo else ""
        return f"gs://{self._bucket(camada)}/{prefixo}"

    async def token_para_kms(self) -> str:
        """Access token with the KMS scope, for PII tokenisation (`ingestao/tokenizacao.py`).

        Kept apart from `token_para_terceiros` on purpose: that one is a STORAGE token, and reusing
        it here gives `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT` - an error that looks like missing
        permission and is not.

        In `autenticacao: "token"` (the secret store holds an already-issued access token) there is
        NO way to request another scope: the token is what it is. Refusing here, with the reason,
        is better than letting the 403 appear in the middle of a publication - the whole
        publication is aborted anyway, and this way the operator knows what to configure.
        """
        if self.autenticacao == "token":
            raise ErroAplicacao(
                codigo="pii_escopo_indisponivel",
                mensagem="The lake connection uses a pre-issued access token, whose scope is "
                "storage and does not work for KMS. To tokenise personal data, the connection "
                "needs the service-account key in the secret store.",
                status=409,
            )
        token, _ = await self._acesso_com_expiracao(_ESCOPO_KMS)
        return token

    async def token_para_terceiros(self) -> tuple[str, int]:
        """`(access_token, expires_at_ms)` for a library that speaks GCS on its own.

        It exists because of Iceberg: PyIceberg and DuckDB need to reach the bucket directly,
        because the manifest stores absolute `gs://` paths. Instead of pointing them at the
        credential (a service-account file on disk, `GOOGLE_APPLICATION_CREDENTIALS`), we hand over
        the **short-lived token** this backend already obtains from the secret store - the secret
        still never touches the disk and no second credential door exists.

        The expiry goes along because the library must know when to renew; returning only the
        token would make a long write die halfway with a 401."""
        token, expira_em = await self._acesso_com_expiracao()
        return token, int(expira_em * 1000)

    def _chave_token(self) -> str | None:
        """Identity under which the token is cached: hash of WHO asks + WHERE it asks.

        `token` mode returns `None` (no caching): the secret in the store already IS the access
        token, there is no exchange to save - and keying by it would put credential material in a
        process dictionary key."""
        if self.autenticacao == "token":
            return None
        cred = self.credencial or {}
        material = f"{cred.get('client_email', '')}\n{cred.get('token_uri', '')}"
        return hashlib.sha256(material.encode()).hexdigest()

    def _invalidar_token(self) -> None:
        """Drop ALL scopes of this credential, not just the one in use.

        The cache key became `credential|scope` when PII tokenisation started requesting a KMS
        token. Continuing to remove by the scope-less key left the entry alive and the GCS 401 no
        longer triggered re-authentication - `test_gcs_reautentica_uma_vez_no_401` caught that.

        Removing both is the right thing, not merely the fix: a 401 usually means a revoked
        credential or a skewed clock, and in those cases the token of the other scope is not valid
        either.
        """
        chave = self._chave_token()
        if chave:
            for k in [x for x in _TOKENS if x == chave or x.startswith(f"{chave}|")]:
                _TOKENS.pop(k, None)

    async def _acesso(self) -> str:
        token, _ = await self._acesso_com_expiracao()
        return token

    async def _acesso_com_expiracao(self, escopo: str = _ESCOPO_GCS) -> tuple[str, float]:
        """Exchange a JWT assertion signed by the service account for an access token (jwt-bearer
        flow). The token is reused until 60 s before its end - re-authenticating per object is what
        makes a backfill hit 429. The cache is at MODULE level (see `_TOKENS`), because the
        instance of this backend is born and dies on every slice.

        `autenticacao: "token"` is the way out for an organisation that BLOCKS service-account keys
        (`constraints/iam.disableServiceAccountKeyCreation`): the secret in the store becomes an
        already-issued **access token**. It expires in ~1 h - fine for a one-off load, not for
        scheduling. The permanent path for a server outside GCP is identity federation (WIF) or
        running ingestion inside GCP.
        """
        agora = time.time()
        cred = self.credencial
        if self.autenticacao == "token":
            token = cred.get("access_token")
            if not token:
                raise ErroAplicacao(
                    codigo="credencial_invalida",
                    mensagem="The connection is in 'token' mode, but the secret store has no "
                    "'access_token'.",
                    status=400,
                )
            # No trustworthy `expires_in`: announce 10 min of validity to whoever needs to renew.
            # If it has already expired, the GCS 401 becomes its own message (see `_erro_http`)
            # instead of "failed to write", which would not tell the operator what to do.
            return token, agora + 600

        # The cache key includes the SCOPE: two scopes are two tokens, and serving the storage one
        # to someone who asked for KMS would return a scope 403 that looks like missing permission.
        chave = f"{self._chave_token()}|{escopo}" if self._chave_token() else None
        cache = _TOKENS.get(chave) if chave else None
        if cache and cache[1] - 60 > agora:
            return cache

        for campo in ("client_email", "private_key", "token_uri"):
            if not cred.get(campo):
                raise ErroAplicacao(
                    codigo="credencial_invalida",
                    mensagem="Incomplete service-account credential (missing "
                    f"'{campo}'). Store the service-account JSON in the connection secret store.",
                    status=400,
                )

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        cabecalho = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        corpo = _b64url(
            json.dumps(
                {
                    "iss": cred["client_email"],
                    "scope": escopo,
                    "aud": cred["token_uri"],
                    "iat": int(agora),
                    "exp": int(agora) + 3600,
                }
            ).encode()
        )
        assinante = serialization.load_pem_private_key(cred["private_key"].encode(), password=None)
        assinatura = _b64url(
            assinante.sign(f"{cabecalho}.{corpo}".encode(), padding.PKCS1v15(), hashes.SHA256())
        )
        asercao = f"{cabecalho}.{corpo}.{assinatura}"

        # Same process-wide client as the bucket requests (`_cliente_http`).
        r = await _cliente_http(self.timeout).post(
            cred["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": asercao,
            },
        )
        if r.status_code != 200:
            # Never echoes the assertion or the key - only the status.
            raise ErroAplicacao(
                codigo="autenticacao_lake",
                mensagem=f"Authentication to the lake storage was refused (HTTP {r.status_code}).",
                status=502,
            )
        dados = r.json()
        entrada = (dados["access_token"], agora + int(dados.get("expires_in", 3600)))
        if chave:
            _TOKENS[chave] = entrada
        return entrada

    # --- Request with retries -----------------------------------------------------------------

    async def _requisitar(
        self,
        metodo: str,
        url: str,
        *,
        acao: str,
        params: dict | None = None,
        content=None,
        headers: dict | None = None,
    ):
        """ONE request to GCS, with a short retry. Every network access of this backend goes
        through here - write, read, exists and test.

        **Why this does not duplicate the workflow-engine retry:** the engine's retry is for the
        whole SLICE, meaning it downloads the public origin again. A 503 on a single GCS PUT would
        cost re-downloading the whole source file - which is precisely what gets an IP blocked.
        This one is per REQUEST and costs nothing externally. The two compose: this one absorbs the
        network hiccup, the engine's covers whatever is left.

        **What it NEVER retries:** 400, 401, 403, 404, 409 and above all **412**. 412 is
        `ifGenerationMatch=0` refusing because the object already exists - insisting would ask
        immutability to stop applying. (It is also exactly what a retry finds when the previous PUT
        DID write and only the response was lost: `motor._gravar_se_novo` already swallows
        `objeto_ja_existe` as a no-op, so the case is handled upstream.)

        Short budget on purpose: 4 attempts capped at 20 s is about 35 s of wall time in the worst
        case, against the 30 min `start_to_close_timeout` of the ingestion activity.
        """
        import httpx

        cfg = get_settings()
        tentativas = max(1, cfg.lake_max_tentativas)
        teto = float(max(0, cfg.lake_backoff_teto_segundos))
        reautenticou = False
        ultimo: object | None = None

        cli = _cliente_http(self.timeout)
        for tentativa in range(tentativas):
            cabecalhos = dict(headers or {})
            cabecalhos["Authorization"] = f"Bearer {await self._acesso()}"
            try:
                # A streaming body arrives as a FACTORY: a consumed iterator cannot be re-read, and
                # each attempt needs the file from the first byte.
                corpo = content() if callable(content) else content
                r = await cli.request(metodo, url, params=params, content=corpo, headers=cabecalhos)
            except httpx.HTTPError as exc:
                ultimo = exc
            else:
                ultimo = r
                if (
                    r.status_code == 401
                    and self.autenticacao == "conta_servico"
                    and not reautenticou
                ):
                    # ONE forced re-authentication: the token may have expired between the margin
                    # check and the call. From the second time on, a 401 means a wrong credential -
                    # insisting only multiplies token requests. In `token` mode there is nothing
                    # to redo: the secret in the store IS the token.
                    reautenticou = True
                    self._invalidar_token()
                    continue
                if r.status_code not in RETENTAVEIS:
                    return r
            if tentativa < tentativas - 1:
                espera_s = espera(
                    tentativa,
                    teto=teto,
                    retry_after=(
                        ultimo.headers.get("Retry-After")
                        if isinstance(ultimo, httpx.Response)
                        else None
                    ),
                )
                await asyncio.sleep(espera_s)

        if isinstance(ultimo, Exception):
            raise ErroAplicacao(
                codigo=_codigo_falha(acao),
                mensagem=f"Failed to {_ACAO_EN.get(acao, acao)} in the lake: {_causa_transporte(ultimo)}.",
                status=502,
            )
        return ultimo

    async def gravar_arquivo(
        self,
        camada: str,
        caminho: str,
        origem: Path,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str:
        """Up to `_MD5_NO_LOOP_ATE` it reads the bytes and writes, as usual. Above that, the file is
        STREAMED, in `_BLOCO_UPLOAD` blocks read on the lake thread: the md5 is computed in one pass
        over the disk and the body goes from disk to network without the whole object in memory -
        a large Parquet used to be read into a single `bytes` per in-flight publication. The file
        is immutable (every snapshot writer writes to a temp file and swaps with `os.replace`), so
        the md5 of the first pass describes what the second one sends; if it does not, GCS refuses
        through `x-goog-hash` and `_conferir_upload` catches the size. The gain from `os.link`
        belongs to the LOCAL backend - here the object travels over the network anyway.

        **It exists because `Protocol` does NOT propagate a default implementation.** The default
        written in `Armazenamento` would only apply to classes inheriting from it, and the backends
        are duck-typed. Without this method, every GCS publication died with `'ArmazenamentoGcs'
        object has no attribute 'gravar_arquivo'` - while a benchmark running on the local backend
        stayed green."""
        tamanho = (await em_thread_lake(origem.stat)).st_size
        if tamanho <= _MD5_NO_LOOP_ATE:
            return await self.gravar(
                camada,
                caminho,
                await em_thread_lake(origem.read_bytes),
                tipo_mime=tipo_mime,
                sobrescrever=sobrescrever,
            )
        md5, tamanho = await em_thread_lake(_md5_e_tamanho_do_arquivo, origem)
        return await self._enviar(
            camada,
            caminho,
            _corpo_do_arquivo(origem),
            md5,
            tamanho,
            tipo_mime=tipo_mime,
            sobrescrever=sobrescrever,
        )

    async def gravar(
        self,
        camada: str,
        caminho: str,
        dados: bytes,
        *,
        tipo_mime: str = "application/octet-stream",
        sobrescrever: bool = False,
    ) -> str:
        # The md5 of a large Parquet (~100 MB) takes ~150 ms: on the loop it would stall heartbeats
        # and the other in-flight publications. `hashlib` releases the GIL, so the lake thread
        # solves it.
        md5 = (
            await em_thread_lake(_md5_b64, dados)
            if len(dados) > _MD5_NO_LOOP_ATE
            else _md5_b64(dados)
        )
        return await self._enviar(
            camada, caminho, dados, md5, len(dados), tipo_mime=tipo_mime, sobrescrever=sobrescrever
        )

    async def _enviar(
        self,
        camada: str,
        caminho: str,
        corpo,
        md5: str,
        tamanho: int,
        *,
        tipo_mime: str,
        sobrescrever: bool,
    ) -> str:
        """The upload itself. `corpo` is `bytes` or an async block-iterator factory
        (`_corpo_do_arquivo`), with `md5`/`tamanho` already computed over the SAME bytes."""
        _validar_sobrescrita(camada, sobrescrever)
        objeto = self._objeto(caminho)
        params = {"uploadType": "media", "name": objeto}
        if not sobrescrever:
            # `ifGenerationMatch=0` = "only if it does not exist yet". Without it the upload is an
            # atomic replacement, which is what the silver pointer needs.
            params["ifGenerationMatch"] = "0"
        if self.chave_kms:
            params["kmsKeyName"] = self.chave_kms

        conferir = get_settings().lake_verificar_checksum
        cabecalhos = {"Content-Type": tipo_mime}
        if callable(corpo):
            # Without it httpx sends `Transfer-Encoding: chunked`; with it, the usual body.
            cabecalhos["Content-Length"] = str(tamanho)
        if conferir:
            # GCS validates on its side and refuses with 400 if the received bytes do not match -
            # which keeps a truncated object from landing in the lake looking complete.
            cabecalhos["x-goog-hash"] = f"md5={md5}"

        url = f"https://storage.googleapis.com/upload/storage/v1/b/{self._bucket(camada)}/o"
        r = await self._requisitar(
            "POST", url, acao="gravar", params=params, content=corpo, headers=cabecalhos
        )
        if r.status_code == 412:
            # ifGenerationMatch=0 refused: the object already exists. The layer is immutable - the
            # caller treats it as a no-op, not as an infrastructure error. (It is also what a retry
            # finds when the previous PUT wrote and only the response was lost.)
            raise ErroAplicacao(
                codigo="objeto_ja_existe",
                mensagem="Object already exists in the lake (the layer is immutable).",
                status=409,
            )
        self._erro_http(r, "gravar")
        if conferir:
            self._conferir_upload(r, tamanho, md5)
        return self.uri(camada, caminho)

    def _conferir_upload(self, r, enviados: int, md5: str) -> None:
        """Check `md5Hash` and `size` of the body the upload returns - which used to be discarded
        (`gravar` only returned the URI).

        Costs nothing: the response already arrived. And it catches the case where `x-goog-hash`
        was not honoured by something on the way, which is exactly when server-side validation
        does not protect.

        A divergence here is **INFRASTRUCTURE, not data**: the bytes were hashed by us before they
        left, and the Parquet does not change its mind - the transport did. So it RAISES (the
        workflow engine retries and the retry fixes it) instead of quarantining, which would make
        the engine give up on something that repeating solves."""
        try:
            corpo = r.json()
        except ValueError:
            return  # response without JSON: nothing to check, and inventing a failure would be worse
        if not isinstance(corpo, dict):
            return
        divergencias: list[str] = []
        recebido = corpo.get("md5Hash")
        if recebido and recebido != md5:
            divergencias.append("md5")
        tamanho = corpo.get("size")
        if tamanho is not None and str(tamanho) != str(enviados):
            divergencias.append(f"size ({tamanho} B written, {enviados} B sent)")
        if divergencias:
            raise ErroAplicacao(
                codigo="falha_gravacao_lake",
                mensagem="The lake confirmed the object with a divergent checksum ("
                + ", ".join(divergencias)
                + "): the bytes changed in transit.",
                status=502,
            )

    def _erro_http(self, r, acao: str) -> None:
        """A 401/403 in `token` mode is almost always an EXPIRED token - saying so saves the hunt."""
        if r.status_code < 400:
            return
        if r.status_code in (401, 403) and self.autenticacao == "token":
            raise ErroAplicacao(
                codigo="token_lake_expirado",
                mensagem="The lake storage refused the credential (HTTP "
                f"{r.status_code}). In 'token' mode the access token lasts about 1 h: "
                "rotate the connection credential.",
                status=502,
            )
        raise ErroAplicacao(
            codigo=_codigo_falha(acao),
            mensagem=f"Failed to {_ACAO_EN.get(acao, acao)} in the lake (HTTP {r.status_code}).",
            status=502,
        )

    async def ler(self, camada: str, caminho: str) -> bytes:
        objeto = quote(self._objeto(caminho), safe="")
        url = f"https://storage.googleapis.com/storage/v1/b/{self._bucket(camada)}/o/{objeto}"
        r = await self._requisitar("GET", url, acao="ler", params={"alt": "media"})
        if r.status_code == 404:
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem="Object not found in the lake.", status=404
            )
        self._erro_http(r, "ler")
        conteudo = r.content
        if get_settings().lake_verificar_checksum:
            # Closes `materializar` WITHOUT a new database column: `ingestao_lotes.sha256` is the
            # hash of the RAW payload downloaded from the origin, before decoding - not of the
            # bronze Parquet. There is no Parquet digest to check against today; what exists is
            # what GCS itself declares in the response.
            esperado = _md5_do_cabecalho(r.headers.get("x-goog-hash"))
            if esperado and esperado != _md5_b64(conteudo):
                raise ErroAplicacao(
                    codigo="falha_ler_lake",
                    mensagem="Checksum mismatch while reading the lake: the received bytes do not "
                    "match the md5 declared by the storage.",
                    status=502,
                )
        return conteudo

    async def existe(self, camada: str, caminho: str) -> bool:
        objeto = quote(self._objeto(caminho), safe="")
        url = f"https://storage.googleapis.com/storage/v1/b/{self._bucket(camada)}/o/{objeto}"
        r = await self._requisitar("GET", url, acao="ler", params={"fields": "name"})
        return r.status_code == 200

    async def copiar(self, camada: str, origem: str, destino: str) -> dict:
        bucket = self._bucket(camada)
        objeto_origem = quote(self._objeto(origem), safe="")
        objeto_destino = quote(self._objeto(destino), safe="")
        base = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o"

        async def _meta(objeto: str):
            r = await self._requisitar(
                "GET",
                f"{base}/{objeto}",
                acao="ler",
                params={"fields": "name,size,md5Hash,crc32c"},
            )
            if r.status_code == 404:
                return None
            self._erro_http(r, "ler")
            return r.json()

        def _equivalentes(a: dict, b: dict) -> bool:
            if str(a.get("size")) != str(b.get("size")):
                return False
            comparaveis = [
                campo
                for campo in ("md5Hash", "crc32c")
                if a.get(campo) not in (None, "") and b.get(campo) not in (None, "")
            ]
            # Size does not prove content. GCS may omit MD5 for some object types, which is why we
            # accept CRC32C, but at least one COMMON digest must exist and every common digest
            # must match.
            return bool(comparaveis) and all(a[campo] == b[campo] for campo in comparaveis)

        def _resultado(meta: dict, *, ja_existia: bool) -> dict:
            return {
                "bytes": int(meta.get("size") or 0),
                "md5": meta.get("md5Hash"),
                "crc32c": meta.get("crc32c"),
                "ja_existia": ja_existia,
            }

        src = await _meta(objeto_origem)
        if src is None:
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem="Object not found in the lake.", status=404
            )
        dst = await _meta(objeto_destino)
        if dst is not None:
            if not _equivalentes(src, dst):
                raise ErroAplicacao(
                    codigo="copia_lake_conflitante",
                    mensagem="The copy destination already exists with different bytes.",
                    status=409,
                )
            return _resultado(src, ja_existia=True)
        r = await self._requisitar(
            "POST",
            f"{base}/{objeto_origem}/copyTo/b/{bucket}/o/{objeto_destino}",
            acao="gravar",
            params={"ifGenerationMatch": "0"},
        )
        venceu_corrida = r.status_code == 412
        if venceu_corrida:
            dst = await _meta(objeto_destino)
        else:
            self._erro_http(r, "gravar")
            dst = r.json()
        if not isinstance(dst, dict) or not _equivalentes(src, dst):
            if venceu_corrida:
                raise ErroAplicacao(
                    codigo="copia_lake_conflitante",
                    mensagem="The destination created during the copy has different bytes.",
                    status=409,
                )
            raise ErroAplicacao(
                codigo="falha_gravacao_lake",
                mensagem="The copy in the lake finished with divergent bytes/checksum.",
                status=502,
            )
        return _resultado(src, ja_existia=venceu_corrida)

    async def materializar(self, camada: str, caminho: str, destino: Path) -> str:
        """Download the object to `destino` (through the backend, never `httpfs`/`gs://`).

        If the file is already there, it does NOT download again: the bronze path is
        content-addressed (`.../{sha12}/dados.parquet`) and the layer is immutable, so the cache
        never goes stale. Written to a temp file + `os.replace` - two workers downloading at the
        same time must not let DuckDB open a half-written Parquet.

        **The local cache is deliberately not re-verified on every read.** The local write is
        already atomic (temp file + `os.replace`), the path is content-addressed and immutable, and
        verifying would require a network round trip per query - exactly the cost the cache exists
        to eliminate. `CFLOW_LAKE_VERIFICAR_CACHE=true` redoes the download (and with it the
        `x-goog-hash` check) to investigate suspected corruption; it is a diagnostic tool, not an
        operating mode."""
        teto = get_settings().lake_cache_max_mib
        if not get_settings().lake_verificar_cache and await em_thread_lake(destino.is_file):
            if teto:
                # A cache hit marks USE: the trimmer deletes by oldest `mtime`.
                await em_thread_lake(_tocar, destino)
            return str(destino)
        # SINGLE FLIGHT per destination: ten references reading the same dataset in the same
        # second made ten downloads of the same object, because the cache only sees the file after
        # it exists - measured on a production-like host: 3-5 s for the concurrent read against ~1 s
        # for the one that found the cache. Whoever waits checks the cache again and leaves without
        # downloading.
        async with VOO_DOWNLOAD.voo(str(destino)):
            if not get_settings().lake_verificar_cache and await em_thread_lake(destino.is_file):
                return str(destino)
            await em_thread_lake(destino.parent.mkdir, parents=True, exist_ok=True)
            tmp = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.parcial")
            try:
                await self._baixar_em_blocos(camada, caminho, tmp)
                await em_thread_lake(os.replace, tmp, destino)
            except BaseException:
                await em_thread_lake(tmp.unlink, missing_ok=True)
                raise
        if teto:
            await em_thread_lake(
                aparar_cache,
                Path(get_settings().lake_cache_dir),
                teto * 1024 * 1024,
                proteger=destino,
            )
        return str(destino)

    async def materializar_confiavel(self, camada: str, caminho: str, destino: Path) -> str:
        """The object backend already materialises into the external destination; no internal path
        is exposed."""
        return await self.materializar(camada, caminho, destino)

    async def materializar_backup_confiavel(self, backup: str, caminho: str, destino: Path) -> str:
        """Physical backups that are siblings of the layers have no implicit equivalent on GCS."""
        raise ErroAplicacao(
            codigo="backup_nao_suportado",
            mensagem="Reading a physical backup of the old layout requires the local backend.",
            status=400,
        )

    async def _baixar_em_blocos(self, camada: str, caminho: str, tmp: Path) -> None:
        """Download the object to `tmp` in `BLOCO_DOWNLOAD` blocks, checking the `x-goog-hash` md5
        against what was WRITTEN, without ever holding the whole object in memory.

        It used to be `self.ler()` + `write_bytes`: a Parquet with a million rows went through the
        process memory on every cache miss - and the `GET /publicados/.../arquivo` endpoint exists
        precisely to serve that size. Retries follow the SAME policy as `_requisitar` (per request,
        a 401 re-authenticates once, only `RETENTAVEIS` repeat); a drop IN THE MIDDLE of the body
        also repeats, and the next attempt rewrites `tmp` from scratch."""
        import httpx

        cfg = get_settings()
        objeto = quote(self._objeto(caminho), safe="")
        url = f"https://storage.googleapis.com/storage/v1/b/{self._bucket(camada)}/o/{objeto}"
        tentativas = max(1, cfg.lake_max_tentativas)
        teto = float(max(0, cfg.lake_backoff_teto_segundos))
        reautenticou = False
        ultimo: object | None = None
        cli = _cliente_http(self.timeout)
        for tentativa in range(tentativas):
            cabecalhos = {"Authorization": f"Bearer {await self._acesso()}"}
            retry_after = None
            try:
                async with cli.stream("GET", url, params={"alt": "media"}, headers=cabecalhos) as r:
                    ultimo = r
                    if (
                        r.status_code == 401
                        and self.autenticacao == "conta_servico"
                        and not reautenticou
                    ):
                        reautenticou = True
                        self._invalidar_token()
                        continue
                    if r.status_code == 404:
                        raise ErroAplicacao(
                            codigo="objeto_ausente",
                            mensagem="Object not found in the lake.",
                            status=404,
                        )
                    if r.status_code not in RETENTAVEIS:
                        self._erro_http(r, "ler")
                        await self._gravar_corpo(r, tmp, cfg.lake_verificar_checksum)
                        return
                    retry_after = r.headers.get("Retry-After")
            except httpx.HTTPError as exc:
                ultimo = exc
            if tentativa < tentativas - 1:
                await asyncio.sleep(espera(tentativa, teto=teto, retry_after=retry_after))
        if isinstance(ultimo, Exception):
            raise ErroAplicacao(
                codigo=_codigo_falha("ler"),
                mensagem=f"Failed to read from the lake: {_causa_transporte(ultimo)}.",
                status=502,
            )
        self._erro_http(ultimo, "ler")

    @staticmethod
    async def _gravar_corpo(r, tmp: Path, conferir: bool) -> None:
        md5 = hashlib.md5(usedforsecurity=False)
        arquivo = await em_thread_lake(open, tmp, "wb")
        try:
            async for bloco in r.aiter_bytes(BLOCO_DOWNLOAD):
                md5.update(bloco)
                await em_thread_lake(arquivo.write, bloco)
        finally:
            await em_thread_lake(arquivo.close)
        esperado = _md5_do_cabecalho(r.headers.get("x-goog-hash")) if conferir else None
        if esperado and esperado != base64.b64encode(md5.digest()).decode():
            raise ErroAplicacao(
                codigo="falha_ler_lake",
                mensagem="Checksum mismatch while reading the lake: the received bytes do not "
                "match the md5 declared by the storage.",
                status=502,
            )

    async def testar(self) -> dict:
        """Validate what INGESTION needs: authenticate and reach the buckets.

        It does not require reading bucket metadata. The ingestion service account has
        `objectAdmin`/`objectCreator` - OBJECT permissions -, so `GET /b/{bucket}` answers 403 even
        with a perfectly good write credential. Failing on that would reject the right
        configuration."""
        try:
            await self._acesso()
        except ErroAplicacao as e:
            return {"ok": False, "causa": e.mensagem}
        detalhes: dict[str, str] = {}
        for camada in CAMADAS:
            nome = (self.buckets or {}).get(camada)
            if not nome:
                continue
            try:
                r = await self._requisitar(
                    "GET",
                    f"https://storage.googleapis.com/storage/v1/b/{nome}",
                    acao="ler",
                    params={"fields": "name"},
                )
            except ErroAplicacao as e:
                # A connection test returns a VERDICT, not an exception: whoever clicked "Test"
                # needs to read the cause on screen.
                return {"ok": False, "causa": e.mensagem}
            if r.status_code == 200:
                detalhes[camada] = "full access"
            elif r.status_code == 403:
                # Expected with object-only permission; writing is proven on the first write.
                detalhes[camada] = "credential accepted (no read access to bucket metadata)"
            elif r.status_code == 404:
                return {"ok": False, "causa": f"bucket '{nome}' does not exist"}
            else:
                return {
                    "ok": False,
                    "causa": f"bucket '{nome}' is unreachable (HTTP {r.status_code})",
                }
        return {"ok": True, "provedor": "gcs", "buckets": detalhes}


# --- Factory -----------------------------------------------------------------------------------


def validar_config(config: dict) -> list[str]:
    """Validate the configuration of an `objeto` connection (called by connection registration)."""
    p: list[str] = []
    if not isinstance(config, dict):
        return ["The configuration must be an object."]
    provedor = config.get("provedor", "local")
    if provedor not in ("local", "gcs"):
        p.append("'provedor' must be 'local' or 'gcs'.")
    if provedor == "gcs" and config.get("autenticacao", "conta_servico") not in (
        "conta_servico",
        "token",
    ):
        p.append("'autenticacao' must be 'conta_servico' or 'token'.")
    if provedor == "gcs":
        buckets = config.get("buckets")
        # A connection with only READ layers (`data_locked`, with its own service account) is not
        # an ingestion destination: requiring landing/bronze/quarantine from it would force
        # inventing buckets.
        so_leitura = (
            isinstance(buckets, dict)
            and bool(buckets)
            and all(k in CAMADAS_SO_LEITURA for k in buckets)
        )
        if not so_leitura and (
            not isinstance(buckets, dict) or not buckets.get("landing") or not buckets.get("bronze")
        ):
            p.append("'buckets' needs at least 'landing' and 'bronze'.")
        elif not so_leitura and not buckets.get("quarantine"):
            p.append(
                "'buckets.quarantine' is required: without a dead-letter queue, a malformed batch vanishes."
            )
        for k in buckets or {}:
            if k not in CAMADAS:
                p.append(f"'buckets.{k}' is not a lake layer ({', '.join(CAMADAS)}).")
    elif provedor == "local" and config.get("raiz") and not str(config["raiz"]).startswith("/"):
        p.append("'raiz' must be an absolute path.")
    return p


def _recusar_fora_do_local(provedor: str) -> None:
    """`CFLOW_LAKE_SOMENTE_LOCAL=true` (dev, CI, test harness): any non-local lake is REFUSED.

    It lives in `montar` because that is the single point through which every lake connection
    becomes a backend - `abrir` (`lake`/`iceberg` output, `lake`/`publicado` source, ingestion,
    stamp) and the "Test connection" action. And it comes before anything else: before resolving
    the credential, building a client and touching the network - refusing after opening the path
    to the bucket would be refusing too late."""
    if provedor != "local" and get_settings().lake_somente_local:
        raise ErroAplicacao(
            codigo="lake_somente_local",
            mensagem=(
                "This environment only uses the LOCAL lake (CFLOW_LAKE_SOMENTE_LOCAL=true), and "
                f"the lake connection points at provider '{provedor}'. Here the lake connection "
                'must be {"provedor": "local"} - with the bucket, a run would write to the lake of '
                "another environment and move the pointer the consumer there reads."
            ),
            status=409,
        )


def montar(tipo: str, config: dict, segredo: str | None) -> Armazenamento:
    """Build the backend from the already-resolved (type, config, secret) of the connection."""
    if tipo != "objeto":
        raise ErroAplicacao(
            codigo="conexao_tipo_sem_lake",
            mensagem=f"A connection of type '{tipo}' is not an object store.",
            status=400,
        )
    provedor = (config or {}).get("provedor", "local")
    _recusar_fora_do_local(provedor)
    if provedor == "local":
        return ArmazenamentoLocal(raiz=Path(config.get("raiz") or get_settings().lake_dir))
    if provedor != "gcs":
        raise ErroAplicacao(
            codigo="provedor_invalido",
            mensagem=f"Unsupported storage provider: '{provedor}'.",
            status=400,
        )
    if not segredo:
        raise ErroAplicacao(
            codigo="credencial_ausente",
            mensagem="The lake connection has no service-account credential in the secret store.",
            status=400,
        )
    autenticacao = (config or {}).get("autenticacao", "conta_servico")
    if autenticacao == "token":
        # The secret is the raw access token (not JSON): the organisation blocks service-account
        # keys.
        credencial = {"access_token": segredo.strip()}
    else:
        try:
            credencial = json.loads(segredo)
        except ValueError:
            raise ErroAplicacao(
                codigo="credencial_invalida",
                mensagem="The lake credential is not a valid service-account JSON.",
                status=400,
            ) from None
    return ArmazenamentoGcs(
        buckets=config.get("buckets") or {},
        credencial=credencial,
        prefixo=config.get("prefixo") or "",
        chave_kms=config.get("chave_kms"),
        timeout=int(config.get("timeout_segundos") or 120),
        autenticacao=autenticacao,
    )


async def abrir(conexao_id: uuid.UUID | str | None) -> Armazenamento:
    """The lake backend. With no connection given -> **local** at the root of `CFLOW_LAKE_DIR`.

    The local default is not a testing convenience: it is what keeps the platform working
    air-gapped. Registering the `objeto` connection is what takes it to GCS."""
    if conexao_id is None:
        return ArmazenamentoLocal(raiz=Path(get_settings().lake_dir))
    chave = str(conexao_id)
    agora = time.monotonic()
    em_cache = _BACKENDS.get(chave)
    if em_cache is not None and agora - em_cache[0] < _TTL_BACKEND_S:
        return em_cache[1]
    from ..conexoes import service as conexoes

    acesso = await conexoes.obter_acesso(uuid.UUID(chave))
    arm = montar(acesso.tipo, acesso.config, acesso.senha)
    if len(_BACKENDS) >= _TETO_BACKENDS:
        _BACKENDS.clear()
    _BACKENDS[chave] = (agora, arm)
    return arm


#: Backend built per connection, per process, with a short TTL. Every lake output and every
#: `publicado` source called `abrir`, and each call was a transaction + a secret-store decryption
#: for the same connection - about a hundred per run, ~40 ms of queueing each. The backends are
#: dataclasses with no per-call state, so the instance is shareable. `editar`/
#: `rotacionar_credencial`/`excluir` forget it immediately IN THIS process (`esquecer`); in the
#: others (api x worker), the TTL is the ceiling on how long a replaced credential stays in use.
#: The credential lives only in the memory of the process that already resolved it - the same
#: exposure as the GCS token cache (`_TOKENS`).
_BACKENDS: dict[str, tuple[float, Armazenamento]] = {}
_TTL_BACKEND_S = 60.0
_TETO_BACKENDS = 256


def esquecer(conexao_id: uuid.UUID | str) -> None:
    """Drop the connection's cached backend (config/credential changed, or it was deleted)."""
    _BACKENDS.pop(str(conexao_id), None)
