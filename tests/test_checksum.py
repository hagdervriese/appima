"""Tests for checksum detection, hashing, and checksum-file parsing."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from appima.core.checksum import (
    compute_file_hash,
    parse_checksum_file,
    parse_checksum_input,
    verify_file_hash,
)
from appima.core.models import AppimaError


class ChecksumTests(unittest.TestCase):
    def test_auto_detect_algorithms_by_length(self) -> None:
        md5_hex = hashlib.md5(b"hello").hexdigest()
        sha256_hex = hashlib.sha256(b"hello").hexdigest()
        sha512_hex = hashlib.sha512(b"hello").hexdigest()

        self.assertEqual(parse_checksum_input(md5_hex), ("md5", md5_hex))
        self.assertEqual(parse_checksum_input(sha256_hex.upper()), ("sha256", sha256_hex))
        self.assertEqual(parse_checksum_input(f"SHA-512:{sha512_hex}"), ("sha512", sha512_hex))

    def test_invalid_checksum_rejected(self) -> None:
        with self.assertRaises(AppimaError):
            parse_checksum_input("not-hex-at-all")
        with self.assertRaises(AppimaError):
            parse_checksum_input("abcdef1234")
        with self.assertRaises(AppimaError):
            parse_checksum_input("md5:" + ("a" * 64))

    def test_verify_file_hash_md5_and_sha256(self) -> None:
        payload = b"appima-test-binary-content" * 100
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.AppImage"
            p.write_bytes(payload)

            expected_md5 = hashlib.md5(payload).hexdigest()
            expected_sha256 = hashlib.sha256(payload).hexdigest()

            self.assertEqual(compute_file_hash(p, "md5"), expected_md5)
            ok, actual, _, algo = verify_file_hash(p, expected_md5)
            self.assertTrue(ok)
            self.assertEqual(algo, "md5")
            self.assertEqual(actual, expected_md5)

            ok2, _, _, algo2 = verify_file_hash(p, f"sha256:{expected_sha256}")
            self.assertTrue(ok2)
            self.assertEqual(algo2, "sha256")

            bad_ok, _, _, _ = verify_file_hash(p, "0" * 32)
            self.assertFalse(bad_ok)

    def test_parse_checksum_file_formats(self) -> None:
        h1 = "a" * 64
        h2 = "b" * 32
        gnu_content = f"{h1}  MyApp-1.2.3-x86_64.AppImage\n{'c'*64} *Other.AppImage\n"
        self.assertEqual(
            parse_checksum_file(gnu_content, "MyApp-1.2.3-x86_64.AppImage"),
            ("sha256", h1),
        )

        bsd_content = f"MD5 (MyApp-1.2.3.AppImage) = {h2}\n"
        self.assertEqual(
            parse_checksum_file(bsd_content, "MyApp-1.2.3.AppImage"),
            ("md5", h2),
        )

        # Bare single-digest file
        self.assertEqual(
            parse_checksum_file(f"{h1}\n", "anything.AppImage"),
            ("sha256", h1),
        )


if __name__ == "__main__":
    unittest.main()
