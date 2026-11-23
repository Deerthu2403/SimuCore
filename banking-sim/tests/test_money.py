"""Business behaviour and money safety, including failures at the worst possible moment."""

import asyncio
import sqlite3
import uuid

import httpx
import pytest


async def test_transfer_moves_money_once(bank):
    t = await bank.token()
    a, b = await bank.account("SIM-00001-01"), await bank.account("SIM-00002-01")
    r = await bank.transfer(t, 1234)
    assert r.status_code == 201, r.text
    assert r.json()["detail"]["fraud"] == "allow"
    assert (await bank.account("SIM-00001-01"))["balance"] == a["balance"] - 1234
    assert (await bank.account("SIM-00002-01"))["balance"] == b["balance"] + 1234
    await bank.audit()


async def test_payment_settles_and_sends_one_receipt(bank):
    r = await bank.pay(await bank.token(), 500)
    assert r.status_code == 201, r.text
    assert r.json()["detail"] == {"fraud": "allow", "settlement": "settled", "receipt": "queued"}
    await asyncio.sleep(0.2)
    stats = (await bank.http("notification", "GET", "/internal/notifications/stats",
                             internal=True)).json()
    assert stats["accepted"] == 1 and stats["delivered"] + stats["suppressed"] == 1
    assert (await bank.account("SIM-CLEARING"))["balance"] == 500


async def test_idempotent_replay_and_conflict(bank):
    t, key = await bank.token(), uuid.uuid4().hex
    first, again = await bank.transfer(t, 100, key=key), await bank.transfer(t, 100, key=key)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["replayed"] and again.json()["id"] == first.json()["id"]
    clash = await bank.transfer(t, 101, key=key)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await bank.audit())["info"]["transactions_completed"] == 1


async def test_same_key_concurrently_moves_money_once(bank):
    t, key = await bank.token(), uuid.uuid4().hex
    before = (await bank.account())["balance"]
    rs = await asyncio.gather(*(bank.transfer(t, 700, key=key) for _ in range(8)))
    assert all(r.status_code in (200, 201) for r in rs), [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert (await bank.account())["balance"] == before - 700
    await bank.audit()


async def test_concurrent_spending_cannot_overdraw(bank):
    t = await bank.token()
    chunk = min((await bank.account())["balance"] // 5 + 1, 1_999_999)
    # Only four of eight fit. Fraud is off so that funds are the only limit being tested.
    bank.down.add("fraud")
    bank.ctx("transaction").settings = bank.settings.__class__(
        **{**bank.settings.__dict__, "fraud_fallback_limit": 10_000_000})
    rs = await asyncio.gather(*(bank.transfer(t, chunk) for _ in range(8)))
    codes = sorted(r.status_code for r in rs)
    assert codes == [201] * 4 + [409] * 4, [r.text for r in rs]
    acct = await bank.account()
    assert acct["balance"] >= 0 and acct["held"] == 0
    await bank.audit()


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"amount": 9_999_999}, 403, "FRAUD_DENY"),
    ({"destination": "SIM-99999-01"}, 404, "NOT_FOUND"),
    ({"destination": "SIM-00001-01"}, 422, "SAME_ACCOUNT"),
    ({"source": "SIM-00003-01"}, 404, "NOT_FOUND"),
    ({"amount": -5}, 422, "INVALID_REQUEST"),
    ({"source": "SIM-CLEARING"}, 422, "INVALID_REQUEST"),
])
async def test_business_rejections(bank, body, status, code):
    before = (await bank.account())["balance"]
    r = await bank.transfer(await bank.token(), **body)
    assert (r.status_code, r.json()["error"]["code"]) == (status, code), r.text
    assert (await bank.account())["balance"] == before
    assert (await bank.audit())["info"]["open_holds"] == 0


async def test_insufficient_funds(bank):
    acct = await bank.account()
    bank.down.add("fraud")
    bank.ctx("transaction").settings = bank.settings.__class__(
        **{**bank.settings.__dict__, "fraud_fallback_limit": 10_000_000})
    r = await bank.transfer(await bank.token(), acct["balance"] + 1)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "INSUFFICIENT_FUNDS")


async def test_declined_payment_releases_the_hold(bank):
    t, before = await bank.token(), await bank.account()
    r = await bank.pay(t, 900, beneficiary="EXT-DECLINE-1")
    assert (r.status_code, r.json()["error"]["code"]) == (402, "PAYMENT_DECLINED")
    assert await bank.account() == before
    await bank.audit()


async def test_customers_cannot_see_or_spend_each_others_money(bank):
    t = await bank.token(1)
    assert (await bank.http("account", "GET", "/accounts/SIM-00002-01",
                            token=t)).status_code == 404
    mine = (await bank.http("account", "GET", "/accounts", token=t)).json()
    assert {a["id"] for a in mine} == {"SIM-00001-01", "SIM-00001-02"}
    other = (await bank.transfer(await bank.token(2), 10, source="SIM-00002-01",
                                 destination="SIM-00003-01")).json()["id"]
    assert (await bank.http("transaction", "GET", f"/transactions/{other}",
                            token=t)).status_code == 404


async def test_authentication_rejects_bad_input(bank):
    async def login(c, s):
        return await bank.http("authentication", "POST", "/login",
                               json={"customer_id": c, "secret": s})
    wrong, unknown = await login("CUST00001", "nope"), await login("CUST99999", "nope")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    good = await bank.token()
    payload, sig = good.split(".")
    for forged in ("x", "a.b", f"{payload}.{'0' * 64}", f"{payload}x.{sig}", "é.é"):
        r = await bank.http("authentication", "POST", "/internal/verify", internal=True,
                            json={"token": forged})
        assert r.status_code == 401, forged


# ------------------------------------------------------- failures midway through


async def test_lost_capture_response_still_completes_exactly_once(bank):
    """The database commits, but the reply never reaches the transaction service."""
    def lose_reply(target, request):
        if target == "account" and request.url.path.endswith("/capture"):
            raise httpx.ReadTimeout("lost", request=request)
    t, before = await bank.token(), (await bank.account())["balance"]
    bank.after_response = lose_reply
    r = await bank.transfer(t, 321)
    bank.after_response = None
    assert r.status_code == 201, r.text
    assert (await bank.account())["balance"] == before - 321
    assert (await bank.audit())["info"]["transactions_completed"] == 1


async def test_late_hold_cannot_take_funds_after_abort(bank):
    """Release arrives first; a hold request that was still in flight must then be refused."""
    async def db(path, **json):
        return await bank.http("database", "POST", f"/internal{path}", internal=True, json=json)
    before = await bank.account()
    assert (await db("/holds/TXN-late/release")).json()["status"] == "released"
    r = await db("/holds", txn_id="TXN-late", account_id="SIM-00001-01", amount=50)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "HOLD_RELEASED")
    assert await bank.account() == before


async def test_retry_with_same_key_resumes_after_outage_before_hold(bank):
    t, key = await bank.token(), uuid.uuid4().hex
    bank.down.add("account")
    assert (await bank.transfer(t, 250, key=key)).status_code == 503
    bank.down.clear()
    before = (await bank.account())["balance"]
    r = await bank.transfer(t, 250, key=key)
    assert r.status_code == 201, r.text
    assert (await bank.account())["balance"] == before - 250
    await bank.audit()


async def test_abandoned_work_is_reconciled(fast_expiry_bank):
    """The orchestrator dies after holding funds. The reconciler must free them."""
    bank, t = fast_expiry_bank, None
    t = await bank.token()
    before = await bank.account()

    def die_after_hold(target, request):
        if target == "account" and request.url.path.endswith("/holds"):
            bank.down.update({"account", "database", "payment"})  # nothing can be cleaned up
    bank.after_response = die_after_hold
    assert (await bank.transfer(t, 4000)).status_code == 503
    bank.after_response = None
    bank.down.clear()
    assert (await bank.account())["held"] == 4000
    await asyncio.sleep(1.2)
    assert await bank.account() == before
    info = (await bank.audit())["info"]
    assert info["transactions_processing"] == 0 and info["open_holds"] == 0


async def test_unsettled_payment_follows_the_ledger(fast_expiry_bank):
    bank = fast_expiry_bank
    t = await bank.token()

    def lose_gateway_after_commit(target, request):
        if target == "account" and request.url.path.endswith("/capture"):
            bank.down.add("payment")
    bank.after_response = lose_gateway_after_commit
    r = await bank.pay(t, 800)
    bank.after_response = None
    bank.down.clear()
    assert r.status_code == 201 and r.json()["detail"]["settlement"] == "pending"
    await asyncio.sleep(1.2)
    assert (await bank.audit())["info"]["payments_authorized_not_settled"] == 0


async def test_audit_really_detects_corruption(bank):
    conn = sqlite3.connect(bank.ctx("database").pool.path)
    conn.execute("UPDATE accounts SET balance=balance+1 WHERE id='SIM-00001-01'")
    conn.commit()
    conn.close()
    r = (await bank.http("database", "GET", "/internal/integrity", internal=True)).json()
    assert not r["ok"] and r["problems"]["balance_vs_ledger_mismatch"] == 1


async def test_data_is_synthetic_and_deterministic(bank, tmp_path):
    from services.database import store
    c = (await bank.http("customer", "GET", "/customers/me", token=await bank.token())).json()
    assert c["name"] == "Synthetic Customer 00001" and c["email"].endswith("@example.invalid")
    dumps = []
    for i in (1, 2):
        conn = store.connect(tmp_path / f"seed{i}.sqlite3")
        store.seed(conn, 7, 5)
        dumps.append([tuple(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")])
        assert store.seed(conn, 7, 5) is False  # re-opening never re-seeds
        conn.close()
    assert dumps[0] == dumps[1]


async def test_double_capture_moves_money_once(bank):
    async def db(path, **json):
        return await bank.http("database", "POST", f"/internal{path}", internal=True, json=json)
    before = (await bank.account())["balance"]
    await db("/holds", txn_id="TXN-twice", account_id="SIM-00001-01", amount=60)
    rs = await asyncio.gather(*(db("/holds/TXN-twice/capture", destination="SIM-00002-01")
                                for _ in range(5)))
    assert [(r.status_code, r.json()["status"]) for r in rs] == [(200, "captured")] * 5
    assert (await db("/holds/TXN-twice/release")).json()["status"] == "captured"
    assert (await bank.account())["balance"] == before - 60
    await bank.audit()


async def test_reconciler_never_fails_a_transaction_whose_hold_is_live(tmp_path):
    """Found by scenario S04: overload delayed the hold, so the transaction was old enough to
    reconcile while its hold was not. It was marked failed, and then the capture committed."""
    from services.database import store
    conn = store.connect(tmp_path / "race.sqlite3")
    store.seed(conn, 1, 2)
    txn = {"id": "TXN-race", "customer_id": "CUST00001", "idempotency_key": "k" * 8,
           "fingerprint": "f", "kind": "transfer", "source": "SIM-00001-01",
           "destination": "SIM-00002-01", "amount": 10}
    store.open_transaction(conn, txn)
    conn.execute("UPDATE transactions SET created = created - 100")  # opened long ago
    store.place_hold(conn, "TXN-race", "SIM-00001-01", 10)  # but held just now

    assert store.reconcile(conn, ttl_s=60)["failed"] == 0
    store.capture_hold(conn, "TXN-race", "SIM-00002-01")
    assert store.integrity(conn)["ok"], store.integrity(conn)
    store.reconcile(conn, ttl_s=60)
    assert store.get_transaction(conn, "TXN-race")["status"] == "completed"

    # And if the hold is abandoned instead, hold and transaction are closed together.
    store.open_transaction(conn, {**txn, "id": "TXN-gone", "idempotency_key": "g" * 8})
    store.place_hold(conn, "TXN-gone", "SIM-00001-01", 10)
    conn.execute("UPDATE transactions SET created = created - 100 WHERE id='TXN-gone'")
    conn.execute("UPDATE holds SET created = created - 100 WHERE txn_id='TXN-gone'")
    assert store.reconcile(conn, ttl_s=60)["failed"] == 1
    assert store.get_transaction(conn, "TXN-gone")["status"] == "failed"
    assert store.integrity(conn)["ok"] and store.get_account(conn, "SIM-00001-01")["held"] == 0
    conn.close()
