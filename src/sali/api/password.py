"""One-way password hashing for API login (password auth).

The API login password is the durable credential the iPhone authenticates with (see
`api/routes/enroll.py` POST /auth/login). It is stored ONE-WAY — a salted scrypt hash on the `sali_state`
singleton — never in plaintext and never reversibly. That is deliberate: a login credential must not be
decryptable, so this does NOT use `config/vault.py` (which is reversible AES/Fernet, the right tool for
secrets Sali must read back, the wrong tool for a password).

scrypt (stdlib `hashlib`) is used because no argon2/bcrypt/passlib is installed, and it is a memory-hard
KDF appropriate for a login secret. The cost parameters are encoded INTO the stored string, so they can be
raised later without invalidating existing hashes. Verification is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

# Memory-hard scrypt cost. n=2**15 (~32 MiB working set) keeps one verify well under a second on the host
# while making offline brute-force of a weak password far harder than a bare SHA-256. maxmem is set
# explicitly because these parameters need ~34 MiB, just over hashlib's 32 MiB default.
_N = 2 ** 15
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """Encode `scrypt$n$r$p$salt$hash` for storage. A fresh random salt every call."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify `password` against an encoded hash. False on any missing/malformed input, so a
    caller can pass the DB value straight in (a never-set NULL simply fails to verify)."""
    if not password or not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_s)
        expected = _unb64(hash_s)
    except (ValueError, TypeError):
        return False
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        dklen=len(expected), maxmem=_MAXMEM)
    return secrets.compare_digest(dk, expected)


__all__ = ["hash_password", "verify_password"]
