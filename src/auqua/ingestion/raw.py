# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""Content-addressed, immutable store of raw provider files (BP4 section 4.1; ADR 0026).

Every byte the platform ingests is first stored here, under
``<root>/sha256/<first two hex digits>/<sha256>``: written to a temporary
file while hashed, flushed with ``fsync`` and moved into place with
``os.replace``. An object is never rewritten; reading re-hashes it, so a
changed file is detected. Archives are read member by member as streams with a
budget on the decompressed bytes and are never extracted to disk (Python
``zipfile`` documentation: "Never extract archives from untrusted sources
without prior inspection"; declared sizes in headers can be false).
"""

import hashlib
import io
import os
import tempfile
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from auqua.ingestion.errors import ChecksumError, FetchError, ParseError
from auqua.storage.files import PRIVATE_FILE, mkdir_private

_CHUNK = 1 << 20
_SHA256_HEX = 64


@dataclass(frozen=True)
class RawFile:
    """A stored raw object: its SHA-256 (lower-case hex) and size in bytes."""

    sha256: str
    size: int


class RawStore:
    """Immutable raw objects addressed by SHA-256."""

    def __init__(self, root: Path) -> None:
        """Use (creating if needed, ``0700``) the store at ``root``."""
        self._root = root
        mkdir_private(root / "sha256")

    def path(self, sha256: str) -> Path:
        """Location of the object ``sha256`` (whether or not it exists)."""
        if len(sha256) != _SHA256_HEX or sha256.strip("0123456789abcdef"):
            raise ValueError(f"not a lower-case SHA-256 hex digest: {sha256!r}")
        return self._root / "sha256" / sha256[:2] / sha256

    def put(self, chunks: Iterable[bytes], *, max_bytes: int) -> RawFile:
        """Store the concatenated ``chunks``; an existing identical object is kept.

        Raises:
            FetchError: more than ``max_bytes`` bytes.
        """
        digest = hashlib.sha256()
        size = 0
        temporary = self._root / "tmp"
        mkdir_private(temporary)
        descriptor, name = tempfile.mkstemp(dir=temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"raw object exceeds {max_bytes} bytes")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            Path(name).chmod(PRIVATE_FILE)
            raw = RawFile(digest.hexdigest(), size)
            target = self.path(raw.sha256)
            mkdir_private(target.parent)
            if target.exists():
                Path(name).unlink()
            else:
                Path(name).replace(target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return raw

    def put_file(self, path: Path, *, max_bytes: int) -> RawFile:
        """Store a copy of the local file ``path``."""
        with path.open("rb") as handle:
            return self.put(iter(lambda: handle.read(_CHUNK), b""), max_bytes=max_bytes)

    def read(self, sha256: str) -> bytes:
        """The bytes of object ``sha256``, re-hashed on read.

        Raises:
            ChecksumError: the object is missing or its content changed.
        """
        target = self.path(sha256)
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            raise ChecksumError(f"raw object {sha256} is missing") from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ChecksumError(f"raw object {sha256} does not match its hash")
        return data


def verify_checksum_file(data: bytes, checksum_text: str, file_name: str) -> None:
    """Check ``data`` against a provider ``.CHECKSUM`` line ``<sha256>  <file name>``.

    Raises:
        ChecksumError: malformed checksum file, other file name, or mismatch.
    """
    parts = checksum_text.split()
    if len(parts) != 2 or len(parts[0]) != _SHA256_HEX:  # noqa: PLR2004 - hash and name
        raise ChecksumError(f"checksum file for {file_name} is malformed")
    expected, named = parts[0].lower(), parts[1].lstrip("*")
    if named != file_name:
        raise ChecksumError(f"checksum file names {named!r}, expected {file_name!r}")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ChecksumError(f"{file_name}: SHA-256 {actual}, provider checksum {expected}")


def read_single_member(data: bytes, member: str, *, max_bytes: int) -> bytes:
    """The decompressed bytes of the archive's only member ``member``.

    Raises:
        ParseError: not a zip archive, other or extra members, or more than
            ``max_bytes`` decompressed bytes (counted while reading).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if names != [member]:
                raise ParseError(f"archive members {names[:5]!r}, expected only {member!r}")
            out = bytearray()
            with archive.open(member) as stream:
                for chunk in iter(lambda: stream.read(_CHUNK), b""):
                    out += chunk
                    if len(out) > max_bytes:
                        raise ParseError(f"{member} decompresses to more than {max_bytes} bytes")
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as error:
        raise ParseError(f"archive unreadable: {error}") from error
    return bytes(out)
