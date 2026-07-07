"""Password hashing + session-token minting for the SWG TCG platform.

Pure crypto -- imports nothing from the project except config (a leaf module).
The wire protocol never sees a password: the launcher/web layer verifies the
password here, then mints an opaque session token that the client forwards and
the server binds to an account at login (see db.bind_session / the lobby seam).
"""
import hashlib
import secrets

import config

PW_ALGO = "pbkdf2_sha256"


def hash_password(password, iterations=None, salt=None):
    """Hash a plaintext password. Returns (salt: bytes, hash: bytes, iterations: int).

    Pass an existing salt/iterations to re-derive for verification; omit for a new password.
    """
    if iterations is None:
        iterations = config.PW_ITERATIONS
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt, h, iterations


def verify_password(password, salt, expected_hash, iterations):
    """Constant-time check of a plaintext password against a stored salt/hash."""
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(h, expected_hash)


def new_session_token():
    """A random opaque session id (== the client --sessionID launch arg)."""
    return secrets.token_hex(16)


def new_challenge():
    """A random opaque challenge (== the client --challenge launch arg)."""
    return secrets.token_hex(16)
