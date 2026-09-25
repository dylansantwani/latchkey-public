"""Decrypting a cookie, against both layouts Chrome writes - built here, not guessed at.

Chrome's macOS cookies are `v10 || AES-128-CBC(plaintext)` with a sixteen-space IV, and
since the domain-binding change the plaintext is `SHA-256(host) || value` rather than the
value alone. Both are in one profile at once, because old rows are not rewritten.

The old code decided between them by decrypting four candidate layouts and keeping
whichever *looked* most like text overall. That is a tie-break, and for a value with any
non-ASCII in it the 32-bytes-shorter candidate can win - which hands back a cookie with
its first 32 characters silently cut off. These build both layouts with a known key and
check the answer is the value, exactly.

    python3 -m unittest tests.test_decrypt -v
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

from latchkey import cookies as ck  # noqa: E402

KEY = bytes(range(16))


def encrypt(value: bytes, host: str | None = None) -> bytes:
    """One `encrypted_value` as Chrome would have written it, in either layout."""
    plain = (hashlib.sha256(host.encode()).digest() if host else b"") + value
    pad = 16 - (len(plain) % 16)
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(ck.SPACES)).encryptor()
    return b"v10" + encryptor.update(plain) + encryptor.finalize()


class BothLayoutsRoundTrip(unittest.TestCase):
    VALUES = [
        b"1",
        b"abc123",
        b"session=9f3a-4b21-8cde",
        b"x" * 31,
        b"y" * 32,
        b"z" * 33,
        b"eyJhbGciOiJIUzI1NiJ9." + b"Q" * 300,
        b'{"a":1,"b":"two"}',
        b"",
    ]

    def test_a_value_with_no_domain_prefix_comes_back_whole(self):
        for value in self.VALUES:
            self.assertEqual(ck.decrypt(encrypt(value), KEY, ".example.com"), value,
                             f"{value[:20]!r}")

    def test_a_value_behind_a_domain_prefix_comes_back_without_it(self):
        for host in (".example.com", "github.com", "accounts.google.com"):
            for value in self.VALUES:
                got = ck.decrypt(encrypt(value, host), KEY, host)
                self.assertEqual(got, value, f"{host} {value[:20]!r}")

    def test_the_prefix_is_found_by_its_shape_when_the_host_is_not_known(self):
        """`host_counts` and any caller reading a row without its host still get it right."""
        for value in self.VALUES:
            if not value:
                continue          # an empty value behind a prefix is genuinely ambiguous
            self.assertEqual(ck.decrypt(encrypt(value, "example.com"), KEY), value,
                             f"{value[:20]!r}")

    def test_a_value_that_is_not_ascii_is_not_truncated_by_the_tie_break(self):
        """The regression: printability scoring preferred the 32-shorter candidate."""
        value = "café ☕ — a value with real UTF-8 in it, plus padding to length".encode()
        self.assertEqual(ck.decrypt(encrypt(value, "example.com"), KEY, "example.com"), value)
        self.assertEqual(ck.decrypt(encrypt(value), KEY, "example.com"), value)

    def test_an_unencrypted_value_is_passed_straight_through(self):
        self.assertEqual(ck.decrypt(b"plain", KEY), b"plain")
        self.assertEqual(ck.decrypt(b"", KEY), b"")

    def test_a_blob_that_is_neither_shape_still_answers_rather_than_raising(self):
        for blob in (b"v10", b"v10xyz", b"v10" + os.urandom(48)):
            self.assertIsInstance(ck.decrypt(blob, KEY, "example.com"), bytes)


class ItIsOnePassNotFour(unittest.TestCase):
    def test_the_common_row_does_not_go_through_the_search(self):
        calls = []
        real = ck._decrypt_by_search
        ck._decrypt_by_search = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            for host in (None, "example.com"):
                ck.decrypt(encrypt(b"session=abc", host), KEY, "example.com")
        finally:
            ck._decrypt_by_search = real
        self.assertEqual(calls, [], "both layouts are decided without searching")

    def test_a_whole_jar_decrypts_quickly(self):
        blobs = [encrypt(b"session=%d-abcdefghijklmnop" % i, "example.com")
                 for i in range(4000)]
        started = time.perf_counter()
        for blob in blobs:
            ck.decrypt(blob, KEY, "example.com")
        spent = time.perf_counter() - started
        self.assertLess(spent, 0.5, f"4000 cookies took {spent * 1000:.0f} ms")


if __name__ == "__main__":
    unittest.main()
