"""The wallet vault: one Robinhood Chain wallet per Discord user.

Custody decision, stated once. Keys live SERVER-SIDE on the data volume,
encrypted at rest, and are decrypted only in memory at the moment of signing.
That is what lets McCap trade on someone's behalf from a Discord button. It also
means whoever controls the host environment (``RHC_WALLET_SECRET``) and the
volume controls every wallet here. Users are trusting the operator; the wallet
creation reply says so.

Encryption: XSalsa20-Poly1305 (``nacl.secret.SecretBox``) under a key derived
from ``RHC_WALLET_SECRET`` with scrypt, a fresh salt and nonce per record, and
the scrypt parameters stored alongside so they can be raised later without
breaking old records. The plaintext carries the owner's Discord id, and the
decrypted key must derive the record's address: a record whose metadata was
edited on disk (to hand one user's key to another) fails both checks. PyNaCl is
already a dependency; a money path earns every new one it takes.

Rules:
  - A record is only ever appended; wallets are never deleted or overwritten,
    and a save that would shrink the file relative to what is on disk refuses.
  - Nothing is written while the vault is "locked" (records exist that the
    current secret cannot open). The failure that motivates this: a rotated
    secret quietly treated as an empty vault, and the next save overwriting
    the file that held everyone's keys.
  - In production the data directory must be a mounted volume
    (``RHC_REQUIRE_MOUNTED_DATA_DIR``); keys minted onto ephemeral disk vanish
    on the next deploy along with the funds sent to them.
  - Private key material is never logged and never leaves this module except
    through ``private_key`` (signing) and ``export_hex`` (the owner asked).
"""

import asyncio
import base64
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils
from eth_account import Account

from ..config import DATA_DIR, RHC_REQUIRE_MOUNTED_DATA_DIR, RHC_WALLET_SECRET, RHC_WALLETS_FILE
from ..logging_setup import log
from ..storage import _atomic_write, _backup_corrupt

# The secret is a server-side random string, not a human password; 32 chars of
# token_urlsafe is ~190 bits. Anything shorter is a typo or a test value.
MIN_SECRET_LEN = 32
_PLAINTEXT_LEN = 8 + 32  # user_id (8 bytes) + private key (32 bytes)


class VaultError(Exception):
    """Something about the vault refuses this operation."""


class VaultLockedError(VaultError):
    """Records exist that the configured secret cannot decrypt."""


@dataclass
class Wallet:
    user_id: int
    address: str
    salt: str          # base64
    nonce: str         # base64
    ciphertext: str    # base64, SecretBox output
    ops: int           # scrypt opslimit used for this record
    mem: int           # scrypt memlimit used for this record
    created_ts: float = field(default_factory=time.time)
    label: str = ""


wallets: List[Wallet] = []
LOCK = asyncio.Lock()
_loaded = False
_unlock_cache: Tuple[Optional[str], Optional[bool]] = (None, None)   # (secret+record fingerprint, result)


def vault_ready() -> bool:
    return len(RHC_WALLET_SECRET) >= MIN_SECRET_LEN


def loaded() -> bool:
    return _loaded


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _derive(salt: bytes, ops: int, mem: int) -> bytes:
    return nacl.pwhash.scrypt.kdf(
        nacl.secret.SecretBox.KEY_SIZE, RHC_WALLET_SECRET.encode("utf-8"), salt,
        opslimit=ops, memlimit=mem,
    )


def _encrypt(user_id: int, key: bytes) -> Dict[str, object]:
    # INTERACTIVE limits: the secret is a long random server string, so the KDF's
    # job is key stretching, not brute-force resistance against a weak password.
    ops, mem = nacl.pwhash.scrypt.OPSLIMIT_INTERACTIVE, nacl.pwhash.scrypt.MEMLIMIT_INTERACTIVE
    salt = nacl.utils.random(nacl.pwhash.scrypt.SALTBYTES)
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    box = nacl.secret.SecretBox(_derive(salt, ops, mem))
    plain = int(user_id).to_bytes(8, "big") + bytes(key)
    ct = box.encrypt(plain, nonce).ciphertext
    return {"salt": _b64(salt), "nonce": _b64(nonce), "ciphertext": _b64(ct), "ops": ops, "mem": mem}


def _decrypt(w: Wallet) -> bytes:
    """The private key for a record, after proving the record is intact."""
    try:
        box = nacl.secret.SecretBox(_derive(_unb64(w.salt), int(w.ops), int(w.mem)))
        plain = box.decrypt(_unb64(w.ciphertext), _unb64(w.nonce))
    except (nacl.exceptions.CryptoError, ValueError) as e:
        raise VaultLockedError(
            "The wallet vault cannot be opened with the configured RHC_WALLET_SECRET "
            "(or the record is corrupt). Nothing will be written until this is fixed."
        ) from e
    if len(plain) != _PLAINTEXT_LEN or int.from_bytes(plain[:8], "big") != int(w.user_id):
        raise VaultError(f"Wallet record for user {w.user_id} does not belong to that user; refusing to use it.")
    key = plain[8:]
    if Account.from_key(key).address.lower() != w.address.lower():
        raise VaultError(f"Wallet record {w.address} does not match its key; refusing to use it.")
    return key


# ---------------- persistence ----------------

async def load() -> None:
    global _loaded, _unlock_cache
    parsed: List[Wallet] = []
    try:
        with open(RHC_WALLETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("not a list")
        for raw in data:
            parsed.append(Wallet(**{k: raw[k] for k in Wallet.__dataclass_fields__ if k in raw}))
        wallets[:] = parsed
        log.info("Loaded %d Robinhood Chain wallet(s)", len(parsed))
    except FileNotFoundError:
        wallets[:] = []
        log.info("No wallet vault at %s; starting empty.", RHC_WALLETS_FILE)
    except Exception:
        # Unreadable is NOT empty. Keep the file, refuse to publish an empty list
        # that the next save would write back over it.
        log.exception("Wallet vault %s is unreadable; wallet operations are blocked", RHC_WALLETS_FILE)
        _backup_corrupt(RHC_WALLETS_FILE)
        _loaded = False
        return
    _loaded = True
    _unlock_cache = (None, None)


def _on_disk_addresses() -> List[str]:
    try:
        with open(RHC_WALLETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(r.get("address", "")).lower() for r in data] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        raise VaultError("The wallet file on disk is unreadable; refusing to overwrite it.")


async def _save() -> None:
    """Write the vault. Every record already on disk must still be in memory;
    a save can only ever add records, never drop one."""
    in_memory = {w.address.lower() for w in wallets}
    missing = [a for a in _on_disk_addresses() if a and a not in in_memory]
    if missing:
        raise VaultError(
            f"The wallet file on disk holds {len(missing)} record(s) that memory does not "
            f"(e.g. {missing[0]}); refusing to overwrite it."
        )
    _atomic_write(RHC_WALLETS_FILE, [asdict(w) for w in wallets])


def _assert_writable() -> None:
    if not vault_ready():
        raise VaultError(f"RHC_WALLET_SECRET is not set (needs at least {MIN_SECRET_LEN} characters).")
    if not _loaded:
        raise VaultError("The wallet vault did not load cleanly; refusing to write.")
    if RHC_REQUIRE_MOUNTED_DATA_DIR and not os.path.ismount(str(DATA_DIR)):
        raise VaultError(
            f"DATA_DIR {DATA_DIR} is not a mounted volume. A wallet created here would vanish on the next "
            "deploy, with its funds. Refusing."
        )
    if wallets:
        _decrypt(wallets[0])  # the current secret must open what is already there


# ---------------- API ----------------

def get(user_id: int) -> Optional[Wallet]:
    for w in wallets:
        if w.user_id == user_id:
            return w
    return None


def count() -> int:
    return len(wallets)


async def create(user_id: int, label: str = "") -> Wallet:
    """Generate and store a wallet for a user who does not have one."""
    global _unlock_cache
    async with LOCK:
        if get(user_id) is not None:
            raise VaultError("You already have a wallet.")
        _assert_writable()
        acct = Account.create()
        record = Wallet(user_id=user_id, address=acct.address, label=label[:40], **_encrypt(user_id, bytes(acct.key)))
        wallets.append(record)
        try:
            await _save()
        except Exception:
            wallets.remove(record)
            raise
        _unlock_cache = (None, None)
        log.info("Created Robinhood Chain wallet %s for user %s", acct.address, user_id)
        return record


def private_key(user_id: int) -> bytes:
    """Decrypt a user's key for signing. Callers must not retain it.

    Synchronous and CPU-bound (scrypt); call it through ``asyncio.to_thread``
    from the event loop.
    """
    w = get(user_id)
    if w is None:
        raise VaultError("No wallet for this user.")
    if not vault_ready():
        raise VaultError("RHC_WALLET_SECRET is not set; keys cannot be opened.")
    return _decrypt(w)


def export_hex(user_id: int) -> str:
    return "0x" + private_key(user_id).hex()


def unlockable() -> bool:
    """Whether the configured secret opens the existing records (or there are none).

    Cached per (secret, first record): the check costs a scrypt derivation, and
    it used to run on every gated command.
    """
    global _unlock_cache
    if not wallets:
        return vault_ready()
    fp = hashlib.sha256(RHC_WALLET_SECRET.encode("utf-8")).hexdigest() + "|" + wallets[0].ciphertext[:16]
    if _unlock_cache[0] == fp and _unlock_cache[1] is not None:
        return _unlock_cache[1]
    try:
        _decrypt(wallets[0])
        result = True
    except VaultError:
        result = False
    _unlock_cache = (fp, result)
    return result
