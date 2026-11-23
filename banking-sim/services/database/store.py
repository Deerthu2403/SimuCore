"""SQLite storage behind the database service.

Money rules enforced here, inside single write transactions:
  * amounts are integers in minor units of XTS, the ISO 4217 code reserved for testing
  * every movement is double-entry, so the ledger always sums to zero
  * a hold reserves funds; it becomes exactly one of ``captured`` or ``released``, never both
  * a customer account can never hold or spend more than its balance
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path

from bankcommon.errors import BankError

EQUITY, CLEARING = "SIM-EQUITY", "SIM-CLEARING"
PBKDF2_ROUNDS = 20_000
TERMINAL = {"completed", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, segment TEXT NOT NULL,
  contact_pref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (
  customer_id TEXT PRIMARY KEY REFERENCES customers(id), salt TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), kind TEXT NOT NULL,
  balance INTEGER NOT NULL, held INTEGER NOT NULL DEFAULT 0 CHECK (held >= 0),
  CHECK (customer_id IS NULL OR balance >= held));
CREATE INDEX IF NOT EXISTS accounts_customer ON accounts(customer_id);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, txn_id TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ledger_account ON ledger(account_id);
CREATE TABLE IF NOT EXISTS holds (
  txn_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, amount INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('held','captured','released')), created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS holds_open ON holds(status, created);
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, destination TEXT NOT NULL,
  amount INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL, UNIQUE (customer_id, idempotency_key));
CREATE INDEX IF NOT EXISTS txn_velocity ON transactions(customer_id, created);
CREATE TABLE IF NOT EXISTS payments (
  txn_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, beneficiary TEXT NOT NULL,
  amount INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
"""


def hash_secret(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), PBKDF2_ROUNDS).hex()


def demo_secret(customer_id: str) -> str:
    """Synthetic login secret. Deliberately derivable: these users are not real people."""
    return f"synthetic-{customer_id}"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=0.6)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class write:
    """``with write(conn):`` runs the block in one BEGIN IMMEDIATE transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def seed(conn: sqlite3.Connection, seed_value: int, customers: int) -> bool:
    """Create synthetic customers once. Returns False if the database is already seeded."""
    conn.executescript(SCHEMA)
    rng = random.Random(seed_value)
    with write(conn):
        if conn.execute("SELECT 1 FROM customers LIMIT 1").fetchone():
            return False
        now, total = time.time(), 0
        conn.execute("INSERT INTO accounts VALUES (?,NULL,'equity',0,0)", (EQUITY,))
        conn.execute("INSERT INTO accounts VALUES (?,NULL,'clearing',0,0)", (CLEARING,))
        for i in range(1, customers + 1):
            c = f"CUST{i:05d}"
            conn.execute("INSERT INTO customers VALUES (?,?,?,?,?)", (
                c, f"Synthetic Customer {i:05d}", f"customer{i:05d}@example.invalid",
                rng.choices(["retail", "premium", "business"], [80, 14, 6])[0],
                rng.choice(["email", "push", "none"])))
            salt = f"{rng.getrandbits(64):016x}"
            conn.execute("INSERT INTO credentials VALUES (?,?,?)",
                         (c, salt, hash_secret(demo_secret(c), salt)))
            for j, kind in enumerate(("current", "savings"), 1):
                a, amount = f"SIM-{i:05d}-{j:02d}", rng.randint(200_000, 5_000_000)
                conn.execute("INSERT INTO accounts VALUES (?,?,?,?,0)", (a, c, kind, amount))
                conn.execute("INSERT INTO ledger(txn_id,account_id,amount,created) "
                             "VALUES ('OPENING',?,?,?)", (a, amount, now))
                total += amount
        conn.execute("UPDATE accounts SET balance=? WHERE id=?", (-total, EQUITY))
        conn.execute("INSERT INTO ledger(txn_id,account_id,amount,created) "
                     "VALUES ('OPENING',?,?,?)", (EQUITY, -total, now))
    return True


def _one(conn: sqlite3.Connection, sql: str, *args) -> dict | None:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _need(row: dict | None, what: str) -> dict:
    if row is None:
        raise BankError(404, "NOT_FOUND", f"{what} not found")
    return row


# ------------------------------------------------------------------------ reads
def get_customer(conn, customer_id: str) -> dict:
    return _need(_one(conn, "SELECT * FROM customers WHERE id=?", customer_id), "customer")


def get_credentials(conn, customer_id: str) -> dict:
    return _need(_one(conn, "SELECT * FROM credentials WHERE customer_id=?", customer_id),
                 "credentials")


def get_account(conn, account_id: str) -> dict:
    return _need(_one(conn, "SELECT * FROM accounts WHERE id=?", account_id), "account")


def list_accounts(conn, customer_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM accounts WHERE customer_id=? ORDER BY id", (customer_id,))]


def velocity(conn, customer_id: str, window_s: float) -> dict:
    return _one(conn, "SELECT COUNT(*) AS count, COALESCE(SUM(amount),0) AS total "
                      "FROM transactions WHERE customer_id=? AND created>? AND status<>'failed'",
                customer_id, time.time() - window_s) or {}


def get_payment(conn, txn_id: str) -> dict:
    return _need(_one(conn, "SELECT * FROM payments WHERE txn_id=?", txn_id), "payment")


def _txn(row: dict) -> dict:
    return {**row, "detail": json.loads(row["detail"])}


def get_transaction(conn, txn_id: str) -> dict:
    return _txn(_need(_one(conn, "SELECT * FROM transactions WHERE id=?", txn_id), "transaction"))


# ----------------------------------------------------------------------- writes
def open_transaction(conn, t: dict) -> dict:
    """Create the transaction, or return the one that already owns this idempotency key."""
    with write(conn):
        cur = conn.execute(
            "INSERT INTO transactions(id,customer_id,idempotency_key,fingerprint,kind,source,"
            "destination,amount,status,created) VALUES (?,?,?,?,?,?,?,?,'processing',?) "
            "ON CONFLICT(customer_id,idempotency_key) DO NOTHING",
            (t["id"], t["customer_id"], t["idempotency_key"], t["fingerprint"], t["kind"],
             t["source"], t["destination"], t["amount"], time.time()))
        row = _one(conn, "SELECT * FROM transactions WHERE customer_id=? AND idempotency_key=?",
                   t["customer_id"], t["idempotency_key"])
    return {"created": cur.rowcount == 1, "transaction": _txn(_need(row, "transaction"))}


def finish_transaction(conn, txn_id: str, status: str, detail: dict) -> dict:
    """Move to a terminal status. The ledger has the last word and terminal rows never change."""
    with write(conn):
        row = _need(_one(conn, "SELECT * FROM transactions WHERE id=?", txn_id), "transaction")
        if row["status"] in TERMINAL:
            return _txn(row)
        hold = _one(conn, "SELECT status FROM holds WHERE txn_id=?", txn_id)
        captured = hold is not None and hold["status"] == "captured"
        if captured != (status == "completed"):
            raise BankError(409, "LEDGER_CONFLICT",
                            f"cannot mark {status}: hold is {hold['status'] if hold else 'absent'}")
        conn.execute("UPDATE transactions SET status=?, detail=? WHERE id=?",
                     (status, json.dumps(detail), txn_id))
    return get_transaction(conn, txn_id)


def place_hold(conn, txn_id: str, account_id: str, amount: int) -> dict:
    with write(conn):
        existing = _one(conn, "SELECT * FROM holds WHERE txn_id=?", txn_id)
        if existing:
            if existing["status"] == "released":
                raise BankError(409, "HOLD_RELEASED", "hold was already released")
            if (existing["account_id"], existing["amount"]) != (account_id, amount):
                raise BankError(409, "HOLD_MISMATCH", "hold exists with different terms")
            return existing
        acct = _need(_one(conn, "SELECT * FROM accounts WHERE id=?", account_id), "account")
        if acct["customer_id"] is None:
            raise BankError(409, "NOT_HOLDABLE", "internal accounts cannot be held")
        if acct["balance"] - acct["held"] < amount:
            raise BankError(409, "INSUFFICIENT_FUNDS", "available balance is too low")
        conn.execute("UPDATE accounts SET held=held+? WHERE id=?", (amount, account_id))
        conn.execute("INSERT INTO holds VALUES (?,?,?,'held',?)",
                     (txn_id, account_id, amount, time.time()))
        return _need(_one(conn, "SELECT * FROM holds WHERE txn_id=?", txn_id), "hold")


def capture_hold(conn, txn_id: str, destination: str) -> dict:
    with write(conn):
        hold = _need(_one(conn, "SELECT * FROM holds WHERE txn_id=?", txn_id), "hold")
        if hold["status"] == "released":
            raise BankError(409, "HOLD_RELEASED", "hold was already released")
        if hold["status"] == "captured":
            return hold
        _need(_one(conn, "SELECT id FROM accounts WHERE id=?", destination), "destination account")
        amount, source, now = hold["amount"], hold["account_id"], time.time()
        conn.execute("UPDATE accounts SET balance=balance-?, held=held-? WHERE id=?",
                     (amount, amount, source))
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, destination))
        conn.executemany("INSERT INTO ledger(txn_id,account_id,amount,created) VALUES (?,?,?,?)",
                         [(txn_id, source, -amount, now), (txn_id, destination, amount, now)])
        conn.execute("UPDATE holds SET status='captured' WHERE txn_id=?", (txn_id,))
        return {**hold, "status": "captured"}


def release_hold(conn, txn_id: str) -> dict:
    """Release if still held. The returned status is the truth: it may say ``captured``."""
    with write(conn):
        hold = _one(conn, "SELECT * FROM holds WHERE txn_id=?", txn_id)
        if hold is None:
            # Tombstone: a hold request that is still in flight must not succeed later.
            conn.execute("INSERT INTO holds VALUES (?,'',0,'released',?)", (txn_id, time.time()))
            return {"txn_id": txn_id, "status": "released"}
        if hold["status"] == "held":
            _release(conn, hold)
            return {**hold, "status": "released"}
        return hold


def _release(conn, hold: dict) -> None:
    conn.execute("UPDATE accounts SET held=held-? WHERE id=?", (hold["amount"], hold["account_id"]))
    conn.execute("UPDATE holds SET status='released' WHERE txn_id=?", (hold["txn_id"],))


def reconcile(conn, ttl_s: float) -> dict:
    """Free abandoned holds and settle transactions whose orchestrator died midway."""
    cutoff = time.time() - ttl_s
    with write(conn):
        stale = [dict(r) for r in conn.execute(
            "SELECT * FROM holds WHERE status='held' AND created<?", (cutoff,))]
        for h in stale:
            _release(conn, h)
        note = json.dumps({"reason": "reconciled"})
        done = conn.execute(
            "UPDATE transactions SET status='completed', detail=? WHERE status='processing' "
            "AND created<? AND id IN (SELECT txn_id FROM holds WHERE status='captured')",
            (note, cutoff)).rowcount
        # Only transactions that can no longer move money: no hold, or a released one. A live
        # hold may be captured at any moment, so its transaction waits until the hold expires.
        orphans = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE status='processing' AND created<? "
            "AND id NOT IN (SELECT txn_id FROM holds WHERE status<>'released')", (cutoff,))]
        for txn_id in orphans:
            # Same tombstone as release_hold: nothing may take funds for it from now on.
            conn.execute("INSERT OR IGNORE INTO holds VALUES (?,'',0,'released',?)",
                         (txn_id, time.time()))
            conn.execute("UPDATE transactions SET status='failed', detail=? WHERE id=?",
                         (note, txn_id))
        # Payments whose capture or void never reached the gateway follow the ledger.
        settled = conn.execute(
            "UPDATE payments SET status='captured' WHERE status='authorized' AND created<? "
            "AND txn_id IN (SELECT txn_id FROM holds WHERE status='captured')", (cutoff,)).rowcount
        voided = conn.execute(
            "UPDATE payments SET status='voided' WHERE status='authorized' AND created<? "
            "AND txn_id IN (SELECT txn_id FROM holds WHERE status='released')", (cutoff,)).rowcount
    return {"released": len(stale), "completed": done, "failed": len(orphans),
            "payments_settled": settled, "payments_voided": voided}


PAYMENT_MOVES = {"authorized": {"captured", "voided"}, "declined": set(), "captured": set(),
                 "voided": set()}


def save_payment(conn, p: dict) -> dict:
    with write(conn):
        row = _one(conn, "SELECT * FROM payments WHERE txn_id=?", p["txn_id"])
        if row is None:
            conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                         (p["txn_id"], p["customer_id"], p["beneficiary"], p["amount"],
                          p["status"], time.time()))
        elif p["status"] in PAYMENT_MOVES[row["status"]]:
            conn.execute("UPDATE payments SET status=? WHERE txn_id=?", (p["status"], p["txn_id"]))
    return get_payment(conn, p["txn_id"])


def integrity(conn) -> dict:
    """Independent audit of the money invariants. All problem counters must be zero."""
    conn.execute("BEGIN")  # one consistent snapshot
    try:
        def n(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        report = {
            "ledger_sum": n("SELECT COALESCE(SUM(amount),0) FROM ledger"),
            "balance_sum": n("SELECT SUM(balance) FROM accounts"),
            "balance_vs_ledger_mismatch": n(
                "SELECT COUNT(*) FROM accounts a WHERE a.balance <> COALESCE("
                "(SELECT SUM(amount) FROM ledger l WHERE l.account_id=a.id),0)"),
            "held_mismatch": n(
                "SELECT COUNT(*) FROM accounts a WHERE a.held <> COALESCE((SELECT SUM(amount) "
                "FROM holds h WHERE h.account_id=a.id AND h.status='held'),0)"),
            "overdrawn": n("SELECT COUNT(*) FROM accounts "
                           "WHERE customer_id IS NOT NULL AND balance < held"),
            "unbalanced_transactions": n(
                "SELECT COUNT(*) FROM (SELECT txn_id FROM ledger WHERE txn_id<>'OPENING' "
                "GROUP BY txn_id HAVING SUM(amount)<>0 OR COUNT(*)<>2)"),
            "completed_without_ledger": n(
                "SELECT COUNT(*) FROM transactions t WHERE status='completed' AND NOT EXISTS "
                "(SELECT 1 FROM ledger l WHERE l.txn_id=t.id)"),
            "failed_with_ledger": n(
                "SELECT COUNT(*) FROM transactions t WHERE status='failed' AND EXISTS "
                "(SELECT 1 FROM ledger l WHERE l.txn_id=t.id)"),
        }
        problems = sum(abs(v) for v in report.values())
        info = {
            "transactions_completed": n(
                "SELECT COUNT(*) FROM transactions WHERE status='completed'"),
            "transactions_failed": n("SELECT COUNT(*) FROM transactions WHERE status='failed'"),
            "transactions_processing": n(
                "SELECT COUNT(*) FROM transactions WHERE status='processing'"),
            "open_holds": n("SELECT COUNT(*) FROM holds WHERE status='held'"),
            "payments_authorized_not_settled": n(
                "SELECT COUNT(*) FROM payments WHERE status='authorized'"),
        }
    finally:
        conn.execute("ROLLBACK")
    return {"ok": problems == 0, "problems": report, "info": info}
