"""Safe local OmniRoute credential decryption helpers.

Decrypts OmniRoute's local ``enc:v1:`` credential envelopes using the
STORAGE_ENCRYPTION_KEY from the user's own ``~/.omniroute/.env`` (local
decode only — decrypted values never leave this machine, and are only
written to the local KiteRouter config when they decrypt successfully).
If the key is unavailable or the envelope fails authentication, the
value is treated as undecryptable and the caller must skip the import
entirely (never transmit encrypted envelopes upstream).
"""
from __future__ import annotations


from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ENC_PREFIX = "enc:v1:"
_SALT = b"omniroute-field-encryption-v1"


class CredentialDecodeError(Exception):
    """Raised when an encrypted credential envelope cannot be decoded."""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def load_storage_encryption_key(env_path: Optional[Path] = None) -> Optional[bytes]:
    """Read STORAGE_ENCRYPTION_KEY from the user's local .omniroute/.env."""
    if env_path is None:
        env_path = Path.home() / ".omniroute" / ".env"
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("STORAGE_ENCRYPTION_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val.encode("utf-8") if val else None
    except Exception:
        return None
    return None


def decrypt_value(encrypted: str, key_material: bytes) -> str:
    """Decrypt a single enc:v1: envelope (AES-256-GCM, hex iv:ciphertext:tag)."""
    if not is_encrypted(encrypted):
        raise CredentialDecodeError("value is not an enc:v1: envelope")
    body = encrypted[len(ENC_PREFIX):]
    parts = body.split(":")
    if len(parts) != 3:
        raise CredentialDecodeError("malformed encrypted value")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        kdf = Scrypt(salt=_SALT, length=32, n=2**14, r=8, p=1)
        aes_key = kdf.derive(key_material)
        iv_hex, ct_hex, tag_hex = parts
        iv = bytes.fromhex(iv_hex)
        ciphertext = bytes.fromhex(ct_hex)
        tag = bytes.fromhex(tag_hex)
        aesgcm = AESGCM(aes_key)
        # AESGCM expects ciphertext||tag as data; iv goes in the nonce argument
        return aesgcm.decrypt(iv, ciphertext + tag, None).decode("utf-8")
    except CredentialDecodeError:
        raise
    except Exception as e:
        raise CredentialDecodeError(f"decryption failed: {type(e).__name__}") from e


def try_decrypt(value: Any, key_material: Optional[bytes]) -> Tuple[Optional[str], Optional[str]]:
    """Return (plaintext, None) on success, or (None, reason) on failure.

    reason values: 'not-encrypted' (passthrough plaintext), 'encrypted-import-unsupported'
    (enc envelope that cannot be decoded), 'invalid-type'.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "invalid-type"
    if is_encrypted(value):
        if key_material is None:
            return None, "encrypted-import-unsupported"
        try:
            return decrypt_value(value, key_material), None
        except CredentialDecodeError:
            return None, "encrypted-import-unsupported"
    plaintext = value.strip()
    if not plaintext:
        return None, "empty"
    return plaintext, None
