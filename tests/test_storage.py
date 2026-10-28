from barometer.crypto import BadPassphrase, db_key_from_private, secret_decrypt
from barometer.deep import DeepLocked, DeepVault

from .conftest import PASS


def test_deep_store_is_encrypted_at_rest(bm, settings):
    bm.unlock(PASS)
    with bm.vault.tx() as c:
        c.execute(
            "INSERT INTO notes (id, seq, created_at, raw_text, input_type) VALUES ('N900', 900, '2026-10-28T09:00:00+00:00', 'Sarah confided in me', 'text')"
        )
    bm.lock()
    blob = settings.deep_path.read_bytes()
    assert b"Sarah" not in blob and b"confided" not in blob
    assert b"SQLite format" not in blob


def test_wrong_passphrase_rejected_and_right_one_reopens(bm, settings):
    vault = DeepVault(settings)
    try:
        vault.unlock("not the passphrase")
        raise AssertionError("should have failed")
    except BadPassphrase:
        pass
    vault.unlock(PASS)
    assert vault.store.scalar("SELECT count(*) FROM notes") == 0


def test_locked_store_refuses_reads(bm):
    try:
        bm.vault.store
        raise AssertionError("should be locked")
    except DeepLocked:
        pass


def test_sealed_inbox_needs_private_key(bm):
    blob = bm.vault.seal({"note": {"raw_text": "secret"}})
    assert b"secret" not in blob
    try:
        bm.vault.open_sealed(blob)
        raise AssertionError("locked vault must not open sealed items")
    except DeepLocked:
        pass
    bm.unlock(PASS)
    assert bm.vault.open_sealed(blob)["note"]["raw_text"] == "secret"


def test_note_survives_restart(bm, settings, llm):
    """M1 'done when': a note saved from the phone survives a reboot."""
    from barometer.core import Barometer

    bm.unlock(PASS)
    note_id = bm.pipeline.capture_text("Lunch with Jean was lovely.")
    bm.lock()
    again = Barometer(settings, llm=llm, inline_jobs=True)
    again.unlock(PASS)
    row = again.vault.store.one("SELECT raw_text FROM notes WHERE id = ?", (note_id,))
    assert row["raw_text"] == "Lunch with Jean was lovely."


def test_passphrase_change(bm, settings):
    bm.vault.change_passphrase(PASS, "a brand new passphrase")
    vault = DeepVault(settings)
    vault.unlock("a brand new passphrase")
    private = vault.verify("a brand new passphrase")
    assert secret_decrypt(db_key_from_private(private), settings.deep_path.read_bytes())
