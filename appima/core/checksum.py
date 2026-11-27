"""Checksum auto-detection, hashing, and checksum-file parsing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from appima.core.models import AppimaError

_LENGTH_TO_ALGO = {
    32: "md5",
    40: "sha1",
    64: "sha256",
    128: "sha512",
}

_SUPPORTED_ALGOS = frozenset(_LENGTH_TO_ALGO.values())
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_BSD_LINE_RE = re.compile(
    r"^(MD5|SHA1|SHA256|SHA512)\s*\((.+?)\)\s*=\s*([0-9a-fA-F]+)\s*$"
)
_GNU_LINE_RE = re.compile(r"^([0-9a-fA-F]{32,128})\s+[*]?(.+?)\s*$")


def parse_checksum_input(raw: str) -> tuple[str, str]:
    """Parse a user-supplied checksum string into (algorithm, lowercase_hex).

    Accepts either prefixed form (`md5:<hex>`, `sha256:<hex>`, `sha-256:<hex>`)
    or a bare hex digest whose algorithm is inferred from its length.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise AppimaError("Checksum value is empty.")

    algo = ""
    digest = cleaned
    if ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        algo = prefix.strip().lower().replace("-", "")
        digest = rest.strip()

    digest = digest.lower()
    if not _HEX_RE.match(digest):
        raise AppimaError(f"Invalid checksum format (expected hexadecimal): {raw!r}")

    inferred = _LENGTH_TO_ALGO.get(len(digest))
    if algo:
        if algo not in _SUPPORTED_ALGOS:
            raise AppimaError(
                f"Unsupported checksum algorithm {algo!r}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_ALGOS))}."
            )
        if inferred and inferred != algo:
            raise AppimaError(
                f"Checksum length ({len(digest)} hex chars) does not match {algo.upper()}."
            )
        return algo, digest

    if not inferred:
        raise AppimaError(
            f"Unrecognized checksum length ({len(digest)} hex chars). "
            "Expected MD5 (32), SHA-1 (40), SHA-256 (64), or SHA-512 (128)."
        )
    return inferred, digest


def compute_file_hash(file_path: Path, algorithm: str = "sha256") -> str:
    """Stream-hash a file in 1 MiB chunks and return lowercase hex digest."""
    algo = algorithm.lower().replace("-", "")
    if algo not in _SUPPORTED_ALGOS:
        raise AppimaError(f"Unsupported hash algorithm: {algorithm}")
    hasher = hashlib.new(algo)
    with file_path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_file_hash(
    file_path: Path, expected_input: str
) -> tuple[bool, str, str, str]:
    """Verify file against expected checksum.

    Returns (matches, actual_hex, expected_hex, algorithm).
    """
    algo, expected_hex = parse_checksum_input(expected_input)
    actual_hex = compute_file_hash(file_path, algo)
    return (actual_hex == expected_hex, actual_hex, expected_hex, algo)


def parse_checksum_file(content: str, target_filename: str) -> tuple[str, str] | None:
    """Extract (algorithm, hex_digest) for target_filename from a checksum file.

    Handles GNU coreutils format (`<hash>  <filename>` / `<hash> *<filename>`),
    BSD format (`SHA256 (filename) = <hash>`), and single-digest files.
    """
    target_base = Path(target_filename).name
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return None

    # Single bare digest file (e.g., MyApp.AppImage.sha256 containing only the hash)
    if len(lines) == 1:
        token = lines[0].split()[0]
        if _HEX_RE.match(token.lower()) and len(token) in _LENGTH_TO_ALGO:
            parts = lines[0].split(None, 1)
            if len(parts) == 1:
                return _LENGTH_TO_ALGO[len(token)], token.lower()

    for line in lines:
        if line.startswith("#"):
            continue
        bsd = _BSD_LINE_RE.match(line)
        if bsd:
            algo_raw, fname, digest = bsd.groups()
            if Path(fname.strip()).name == target_base:
                return parse_checksum_input(f"{algo_raw}:{digest}")

        gnu = _GNU_LINE_RE.match(line)
        if gnu:
            digest, fname = gnu.groups()
            fname_clean = fname.strip().lstrip("./")
            if fname_clean == target_filename or Path(fname_clean).name == target_base:
                return parse_checksum_input(digest)

    return None
