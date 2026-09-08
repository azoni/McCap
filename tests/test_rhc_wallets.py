"""The wallet vault: create, encrypt at rest, decrypt for signing, never lose keys."""

import json
import os

import pytest
from eth_account import Account

from mccapbot.rhc import wallets

OTHER_SECRET = "a-different-secret-0123456789-abcdefghij"


@pytest.fixture(autouse=True)
def fresh_vault():
    wallets.wallets.clear()
    wallets._loaded = True
    wallets._unlock_cache = (None, None)
    for suffix in ("", ".corrupt"):
        try:
            os.remove(wallets.RHC_WALLETS_FILE + suffix)
        except FileNotFoundError:
            pass
    yield
    wallets.wallets.clear()
    wallets._loaded = True
    wallets._unlock_cache = (None, None)
    for suffix in ("", ".corrupt"):
        try:
            os.remove(wallets.RHC_WALLETS_FILE + suffix)
        except FileNotFoundError:
            pass


@pytest.mark.asyncio
async def test_create_then_decrypt_gives_back_the_same_key():
    w = await wallets.create(1, "alice")
    assert wallets.get(1) is w
    assert w.address.startswith("0x") and len(w.address) == 42
    key = wallets.private_key(1)
    assert len(key) == 32
    assert Account.from_key(key).address == w.address
    assert wallets.export_hex(1) == "0x" + key.hex()


@pytest.mark.asyncio
async def test_key_material_is_not_in_the_file():
    w = await wallets.create(1)
    key_hex = wallets.private_key(1).hex()
    raw = open(wallets.RHC_WALLETS_FILE, encoding="utf-8").read()
    assert w.address in raw
    assert key_hex not in raw and key_hex.upper() not in raw
    assert "ciphertext" in raw and "salt" in raw and "nonce" in raw


@pytest.mark.asyncio
async def test_one_wallet_per_user():
    await wallets.create(1)
    with pytest.raises(wallets.VaultError):
        await wallets.create(1)
    assert wallets.count() == 1


@pytest.mark.asyncio
async def test_every_record_gets_its_own_salt_and_nonce():
    a = await wallets.create(1)
    b = await wallets.create(2)
    assert a.salt != b.salt and a.nonce != b.nonce and a.ciphertext != b.ciphertext


@pytest.mark.asyncio
async def test_wrong_secret_locks_the_vault_and_blocks_writes(monkeypatch):
    """The failure this prevents: a rotated secret quietly treated as an empty
    vault, and the next save overwriting the file that held everyone's keys."""
    await wallets.create(1)
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", OTHER_SECRET)
    assert not wallets.unlockable()
    with pytest.raises(wallets.VaultLockedError):
        wallets.private_key(1)
    with pytest.raises(wallets.VaultLockedError):
        await wallets.create(2)
    assert wallets.get(2) is None and wallets.count() == 1


@pytest.mark.asyncio
async def test_unlockable_is_cached_per_secret(monkeypatch):
    await wallets.create(1)
    calls = []
    real = wallets._decrypt

    def counting(w):
        calls.append(1)
        return real(w)
    monkeypatch.setattr(wallets, "_decrypt", counting)
    assert wallets.unlockable() and wallets.unlockable() and wallets.unlockable()
    assert len(calls) == 1, "scrypt must not run on every gated command"
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", OTHER_SECRET)
    assert not wallets.unlockable()
    assert len(calls) == 2, "a changed secret is re-checked"


@pytest.mark.asyncio
async def test_short_or_missing_secret_refuses_to_create(monkeypatch):
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", "short-but-not-quite-32")
    assert not wallets.vault_ready()
    with pytest.raises(wallets.VaultError):
        await wallets.create(1)
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", "")
    with pytest.raises(wallets.VaultError):
        await wallets.create(1)


@pytest.mark.asyncio
async def test_tampered_metadata_is_refused():
    """Editing the file to hand one user's key to another must fail both checks."""
    a = await wallets.create(1)
    b = await wallets.create(2)
    # Reassign a's encrypted key to user 2's record.
    b.salt, b.nonce, b.ciphertext = a.salt, a.nonce, a.ciphertext
    with pytest.raises(wallets.VaultError, match="does not belong"):
        wallets.private_key(2)
    # Same owner, but the address field was edited.
    a.address = "0x" + "9" * 40
    with pytest.raises(wallets.VaultError, match="does not match its key"):
        wallets.private_key(1)


@pytest.mark.asyncio
async def test_save_refuses_to_shrink_the_file_on_disk():
    await wallets.create(1)
    await wallets.create(2)
    wallets.wallets.pop()          # memory lost a record somehow
    with pytest.raises(wallets.VaultError, match="memory does not"):
        await wallets.create(3)
    assert len(json.load(open(wallets.RHC_WALLETS_FILE, encoding="utf-8"))) == 2, "the file was left alone"


@pytest.mark.asyncio
async def test_unmounted_data_dir_refuses_to_mint_keys(monkeypatch):
    monkeypatch.setattr(wallets, "RHC_REQUIRE_MOUNTED_DATA_DIR", True)
    monkeypatch.setattr(wallets.os.path, "ismount", lambda p: False)
    with pytest.raises(wallets.VaultError, match="not a mounted volume"):
        await wallets.create(1)
    monkeypatch.setattr(wallets.os.path, "ismount", lambda p: True)
    await wallets.create(1)


@pytest.mark.asyncio
async def test_load_reads_back_what_was_saved():
    w = await wallets.create(1, "alice")
    key = wallets.private_key(1)
    wallets.wallets.clear()
    wallets._loaded = False
    await wallets.load()
    assert wallets.loaded()
    again = wallets.get(1)
    assert again is not None and again.address == w.address and again.label == "alice"
    assert wallets.private_key(1) == key


@pytest.mark.asyncio
async def test_unreadable_vault_is_not_treated_as_empty():
    with open(wallets.RHC_WALLETS_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    await wallets.load()
    assert not wallets.loaded()
    with pytest.raises(wallets.VaultError):
        await wallets.create(1)
    assert open(wallets.RHC_WALLETS_FILE, encoding="utf-8").read() == "{not json"
    assert os.path.exists(wallets.RHC_WALLETS_FILE + ".corrupt")


@pytest.mark.asyncio
async def test_missing_file_is_a_clean_empty_vault():
    await wallets.load()
    assert wallets.loaded() and wallets.count() == 0 and wallets.unlockable()


def test_private_key_for_unknown_user_is_an_error():
    with pytest.raises(wallets.VaultError):
        wallets.private_key(999)
