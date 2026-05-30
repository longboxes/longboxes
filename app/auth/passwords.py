"""Argon2id password hashing.

We use ``argon2-cffi``'s ``PasswordHasher`` with library defaults, which the
maintainers tune as recommendations evolve. This is intentional — rolling our
own parameters tends to age badly.

Hashes are self-describing (they carry their parameters in the encoded string),
so ``verify_password`` keeps working when defaults change. ``needs_rehash``
lets us transparently upgrade an old hash on a successful login.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with argon2id."""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True iff ``plaintext`` matches ``hashed``.

    Returns False (rather than raising) on any verification failure so that
    callers can use the result as a boolean without exception handling.
    """
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True if the stored hash should be re-hashed with current parameters.

    Call this after a successful verification; if true, hash the plaintext
    again and persist the new value.
    """
    return _hasher.check_needs_rehash(hashed)
