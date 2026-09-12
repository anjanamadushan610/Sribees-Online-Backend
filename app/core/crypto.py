"""
Encryption for secrets we have to store and later use.

Some third-party credentials cannot be hashed the way a password is: we have to
send the original value to SribeesExpress on every call, so it has to be
recoverable. Recoverable and plaintext are not the same thing, and this module
is the difference — a stolen database dump yields ciphertext, not working
merchant keys.

**Where the master key lives, and why it is not in the database.** Encryption
only helps if the key that opens the data is somewhere the data is not. Storing
it alongside the ciphertext would mean anyone who reads the table reads the
credentials, which is exactly the situation this exists to prevent. So the
master key comes from the environment; the credentials it protects do not.
That is one secret in the deployment's config instead of one per branch per
environment, and it is the only thing an operator has to keep safe.

``CREDENTIAL_ENCRYPTION_KEY`` is the intended source: a urlsafe-base64 32-byte
Fernet key, generated once with ``Fernet.generate_key()``. When it is unset the
key is derived from ``JWT_SECRET_KEY`` instead, so an existing deployment keeps
working with no new configuration — at the cost of tying the two together, and
rotating the JWT secret would then make stored credentials undecryptable.
Setting the dedicated variable is worth doing before any real key is stored.

Ciphertext is written as ``enc:v1:<token>`` — self-describing, so a value that
predates encryption is recognisable on sight, and so a future scheme can be
introduced alongside this one rather than by guessing at the format.
"""
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from app.config.settings import settings

PREFIX = "enc:v1:"

#: Derivation label. Changing it changes every derived key, so don't.
_DERIVE_INFO = b"sribees.credential-encryption.v1"

_fernet: Optional[Fernet] = None


class DecryptionError(Exception):
    """Stored ciphertext could not be opened with the configured master key."""


def _master_key() -> bytes:
    """
    The Fernet key, from config or derived from the JWT secret.

    Derivation is a single SHA-256 over a labelled secret rather than a full
    KDF: the input is already a high-entropy random secret, not a password, so
    there is nothing here for a slow hash to defend against.
    """
    configured = (getattr(settings, "credential_encryption_key", "") or "").strip()
    if configured:
        return configured.encode()

    seed = (settings.jwt_secret_key or "").encode()
    if not seed:
        raise RuntimeError(
            "Neither CREDENTIAL_ENCRYPTION_KEY nor JWT_SECRET_KEY is set; "
            "stored credentials cannot be encrypted or read."
        )
    digest = hashlib.sha256(_DERIVE_INFO + seed).digest()
    return base64.urlsafe_b64encode(digest)


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_master_key())
    return _fernet


def reset_cipher_cache() -> None:
    """Forget the cached cipher. For tests that change the configured key."""
    global _fernet
    _fernet = None


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(value: Optional[str]) -> Optional[str]:
    """
    Encrypt a secret for storage. ``None``/empty passes through unchanged so
    "no value" stays distinguishable from "an encrypted empty string".

    Re-encrypting an already-encrypted value is a no-op, which keeps a caller
    that reads-modifies-writes a row from wrapping it twice.
    """
    if not value:
        return None
    if is_encrypted(value):
        return value
    return PREFIX + _cipher().encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    """
    Recover a stored secret.

    A value without the prefix is returned as-is: rows written before this
    module existed are plaintext, and refusing to read them would take a
    working deployment down rather than quietly upgrading it on next write.
    The warning is deliberate — it should be seen and fixed, not lived with.

    Raises ``DecryptionError`` when a value IS ours but will not open, which
    means the master key changed. That must be loud: silently treating it as
    "no key configured" would send a merchant's parcels somewhere else.
    """
    if not value:
        return None
    if not is_encrypted(value):
        logger.warning(
            "Read a credential that is not encrypted at rest; re-save it to encrypt."
        )
        return value
    try:
        return _cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "A stored credential could not be decrypted. CREDENTIAL_ENCRYPTION_KEY "
            "(or JWT_SECRET_KEY, if it is being derived from that) has changed since "
            "the credential was saved."
        ) from exc
