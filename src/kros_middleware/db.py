"""SQLite storage for webhook events, approvals and the proxy audit log.

All access goes through one connection guarded by a lock and executed in a
worker thread, which is plenty for the volumes a single company produces.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    entity_type INTEGER,
    company_id INTEGER,
    request_id TEXT,
    top_status INTEGER,
    event_id TEXT,
    invoice_id INTEGER,
    send_status TEXT,
    channel TEXT,
    occurred_at TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_request ON webhook_events(request_id);
CREATE INDEX IF NOT EXISTS ix_events_invoice ON webhook_events(invoice_id);
CREATE INDEX IF NOT EXISTS ix_events_received ON webhook_events(received_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    invoice_id INTEGER NOT NULL,
    client TEXT NOT NULL,
    request_body TEXT NOT NULL,
    summary TEXT,
    snapshot TEXT,
    content_hash TEXT,
    document_number TEXT,
    numbering_sequence TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    result_status INTEGER,
    result_body TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_invoice ON approvals(invoice_id, status);
-- At most one open (not yet finished) approval per invoice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_open ON approvals(invoice_id)
    WHERE status IN ('pending', 'approved', 'sending');
CREATE INDEX IF NOT EXISTS ix_approvals_created ON approvals(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    client TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query TEXT,
    status INTEGER,
    request_id TEXT,
    body_sha256 TEXT,
    body TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_log(request_id);

-- Uploads that name a document number, tracked until KROS reports the result, so an
-- approval can never be decided while a write to the same invoice is still in flight.
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    doc_sequence TEXT NOT NULL,
    doc_number TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_uploads_request ON uploads(request_id);
CREATE INDEX IF NOT EXISTS ix_uploads_open ON uploads(done_at, created_at);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        if self.path != ":memory:":
            parent = Path(self.path).parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Create the file owner-only before SQLite touches it.
            if Path(self.path).is_symlink():
                raise RuntimeError(f"refusing to use a symlink as the database: {self.path}")
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            finally:
                os.close(fd)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA secure_delete=ON")
        conn.executescript(SCHEMA)
        self._conn = conn
        if self.path != ":memory:":
            for suffix in ("-wal", "-shm"):
                extra = Path(self.path + suffix)
                if extra.exists():
                    os.chmod(extra, stat.S_IRUSR | stat.S_IWUSR)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(self, fn, *args):
        with self._lock:
            assert self._conn is not None, "database not open"
            return fn(self._conn, *args)

    async def run(self, fn, *args):
        return await asyncio.to_thread(self._run, fn, *args)

    # ------------------------------------------------------------------ webhook events
    async def insert_event(self, row: dict[str, Any]) -> bool:
        """Insert an event; return False if it was a duplicate."""

        def _insert(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """INSERT OR IGNORE INTO webhook_events
                   (received_at, dedupe_key, entity_type, company_id, request_id, top_status,
                    event_id, invoice_id, send_status, channel, occurred_at, payload)
                   VALUES (:received_at, :dedupe_key, :entity_type, :company_id, :request_id, :top_status,
                           :event_id, :invoice_id, :send_status, :channel, :occurred_at, :payload)""",
                row,
            )
            return cur.rowcount == 1

        return await self.run(_insert)

    async def events_by_request(self, request_id: str) -> list[dict[str, Any]]:
        def _q(conn):
            rows = conn.execute(
                "SELECT * FROM webhook_events WHERE request_id = ? ORDER BY id", (request_id,)
            ).fetchall()
            return [_event_dict(r) for r in rows]

        return await self.run(_q)

    async def send_status_events(self, invoice_id: int) -> list[dict[str, Any]]:
        def _q(conn):
            rows = conn.execute(
                """SELECT * FROM webhook_events WHERE invoice_id = ? AND entity_type = 4
                   ORDER BY occurred_at, id""",
                (invoice_id,),
            ).fetchall()
            return [_event_dict(r) for r in rows]

        return await self.run(_q)

    async def events_since(
        self, since: str, entity_type: int | None, limit: int, after_id: int
    ) -> list[dict[str, Any]]:
        def _q(conn):
            sql = "SELECT * FROM webhook_events WHERE received_at >= ? AND id > ?"
            params: list[Any] = [since, after_id]
            if entity_type is not None:
                sql += " AND entity_type = ?"
                params.append(entity_type)
            sql += " ORDER BY id LIMIT ?"
            params.append(limit)
            return [_event_dict(r) for r in conn.execute(sql, params).fetchall()]

        return await self.run(_q)

    # ------------------------------------------------------------------ approvals
    async def create_approval(self, row: dict[str, Any]) -> None:
        def _ins(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE approvals SET status = 'expired' WHERE invoice_id = ? AND status = 'pending' "
                    "AND expires_at <= ?",
                    (row["invoice_id"], row["created_at"]),
                )
                _insert_approval(conn, row)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        await self.run(_ins)

    async def create_approval_capped(self, row: dict[str, Any], max_pending: int) -> bool:
        """Insert an approval unless max_pending unexpired approvals are already open (one transaction)."""

        def _ins(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE approvals SET status = 'expired' WHERE invoice_id = ? AND status = 'pending' "
                    "AND expires_at <= ?",
                    (row["invoice_id"], row["created_at"]),
                )
                pending = conn.execute(
                    "SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND expires_at > ?", (row["created_at"],)
                ).fetchone()[0]
                if pending >= max_pending:
                    conn.execute("ROLLBACK")
                    return False
                _insert_approval(conn, row)
                conn.execute("COMMIT")
                return True
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        return await self.run(_ins)

    # ------------------------------------------------------------------ in-flight uploads
    async def add_uploads(self, keys: list[tuple[str, str]], now: str) -> list[int]:
        def _ins(conn):
            ids = []
            for seq, num in keys:
                cur = conn.execute(
                    "INSERT INTO uploads (doc_sequence, doc_number, created_at) VALUES (?, ?, ?)", (seq, num, now)
                )
                ids.append(cur.lastrowid)
            return ids

        return await self.run(_ins)

    async def settle_uploads(self, ids: list[int], request_id: str | None, done_at: str | None) -> None:
        """Attach KROS's requestId to tracked uploads, or close them (``done_at``) when KROS won't process them.

        The result webhook may arrive before the forwarding code gets here. Setting the requestId and
        checking for an already stored result happen in one transaction, and the webhook stores its event
        and closes uploads in one transaction too, so a result is never missed whatever the order.
        """

        def _upd(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "UPDATE uploads SET request_id = ?, done_at = ? WHERE id = ?",
                    [(request_id, done_at, i) for i in ids],
                )
                if request_id and done_at is None:
                    seen = conn.execute(
                        "SELECT received_at FROM webhook_events WHERE request_id = ? ORDER BY id LIMIT 1", (request_id,)
                    ).fetchone()
                    if seen is not None:
                        conn.executemany(
                            "UPDATE uploads SET done_at = ? WHERE id = ? AND done_at IS NULL",
                            [(seen[0], i) for i in ids],
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        if ids:
            await self.run(_upd)

    async def insert_event_and_finish_uploads(self, row: dict[str, Any], now: str) -> bool:
        """Store a webhook event and close the uploads of its request in one transaction.

        Returns False if the event was a duplicate (uploads are closed either way).
        """

        def _tx(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO webhook_events
                       (received_at, dedupe_key, entity_type, company_id, request_id, top_status,
                        event_id, invoice_id, send_status, channel, occurred_at, payload)
                       VALUES (:received_at, :dedupe_key, :entity_type, :company_id, :request_id, :top_status,
                               :event_id, :invoice_id, :send_status, :channel, :occurred_at, :payload)""",
                    row,
                )
                if row.get("request_id"):
                    conn.execute(
                        "UPDATE uploads SET done_at = ? WHERE request_id = ? AND done_at IS NULL",
                        (now, row["request_id"]),
                    )
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        return await self.run(_tx)

    async def open_upload_keys(self) -> list[tuple[str, str]]:
        """Document keys of uploads without a result. They stay open until KROS reports or an operator resolves them."""
        return await self.run(
            lambda conn: [
                (r[0], r[1])
                for r in conn.execute("SELECT doc_sequence, doc_number FROM uploads WHERE done_at IS NULL").fetchall()
            ]
        )

    async def list_open_uploads(self) -> list[dict[str, Any]]:
        return await self.run(
            lambda conn: [
                dict(r)
                for r in conn.execute(
                    "SELECT id, request_id, doc_sequence, doc_number, created_at FROM uploads "
                    "WHERE done_at IS NULL ORDER BY id"
                ).fetchall()
            ]
        )

    async def resolve_upload(self, upload_id: int, now: str) -> bool:
        """Close an open upload by hand, after checking in KROS what happened to it."""
        return await self.run(
            lambda conn: (
                conn.execute(
                    "UPDATE uploads SET done_at = ? WHERE id = ? AND done_at IS NULL", (now, upload_id)
                ).rowcount
                == 1
            )
        )

    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        def _q(conn):
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return dict(row) if row else None

        return await self.run(_q)

    async def open_approval_for_invoice(self, invoice_id: int, now: str) -> dict[str, Any] | None:
        """Return a pending/approved/sending approval for this invoice, if any."""

        def _q(conn):
            row = conn.execute(
                """SELECT * FROM approvals WHERE invoice_id = ?
                   AND (status IN ('approved', 'sending') OR (status = 'pending' AND expires_at > ?))
                   ORDER BY created_at DESC LIMIT 1""",
                (invoice_id, now),
            ).fetchone()
            return dict(row) if row else None

        return await self.run(_q)

    async def transition_approval(
        self,
        approval_id: str,
        from_status: str,
        to_status: str,
        now: str,
        decided_by: str | None = None,
        require_unexpired: bool = False,
    ) -> bool:
        """Atomically move an approval between states. Returns True if this call won."""

        def _upd(conn):
            sql = "UPDATE approvals SET status = ?"
            params: list[Any] = [to_status]
            if decided_by is not None:
                sql += ", decided_at = ?, decided_by = ?"
                params += [now, decided_by]
            sql += " WHERE id = ? AND status = ?"
            params += [approval_id, from_status]
            if require_unexpired:
                sql += " AND expires_at > ?"
                params.append(now)
            return conn.execute(sql, params).rowcount == 1

        return await self.run(_upd)

    async def finish_approval(
        self, approval_id: str, from_status: str, status: str, result_status: int | None, result_body: str
    ) -> bool:
        def _upd(conn):
            return (
                conn.execute(
                    "UPDATE approvals SET status = ?, result_status = ?, result_body = ? WHERE id = ? AND status = ?",
                    (status, result_status, result_body, approval_id, from_status),
                ).rowcount
                == 1
            )

        return await self.run(_upd)

    async def count_pending(self, now: str) -> int:
        return await self.run(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND expires_at > ?", (now,)
            ).fetchone()[0]
        )

    async def frozen_documents(self) -> list[tuple[str, str]]:
        """(numbering_sequence, document_number) of invoices that must not be overwritten.

        That is every invoice with an open approval, and every invoice whose send was
        submitted or whose outcome is unknown: KROS sends asynchronously, and the
        customer's e-mail links to the live document, so a later overwrite would
        change what the customer sees.
        """

        def _q(conn):
            rows = conn.execute(
                """SELECT numbering_sequence, document_number FROM approvals
                   WHERE status IN ('pending', 'approved', 'sending', 'submitted', 'unknown')
                   AND document_number IS NOT NULL"""
            ).fetchall()
            return [(r[0] or "", r[1]) for r in rows]

        return await self.run(_q)

    async def previous_send(self, invoice_id: int) -> dict[str, Any] | None:
        def _q(conn):
            row = conn.execute(
                """SELECT status, decided_at FROM approvals WHERE invoice_id = ?
                   AND status IN ('submitted', 'unknown') ORDER BY created_at DESC LIMIT 1""",
                (invoice_id,),
            ).fetchone()
            return {"status": row[0], "at": row[1]} if row else None

        return await self.run(_q)

    async def mark_interrupted_sends(self) -> int:
        """Called at startup: a send that was in flight when we stopped has an unknown outcome."""
        return await self.run(
            lambda conn: (
                conn.execute(
                    "UPDATE approvals SET status = 'unknown', result_body = 'interrupted by restart' "
                    "WHERE status IN ('approved', 'sending')"
                ).rowcount
            )
        )

    async def list_approvals(self, client: str | None, limit: int) -> list[dict[str, Any]]:
        def _q(conn):
            if client is None:
                rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE client = ? ORDER BY created_at DESC LIMIT ?", (client, limit)
                ).fetchall()
            return [dict(r) for r in rows]

        return await self.run(_q)

    async def expire_approvals(self, now: str) -> list[dict[str, Any]]:
        def _upd(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status = 'pending' AND expires_at <= ?", (now,)
                ).fetchall()
                conn.execute(
                    "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?", (now,)
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return [dict(r) for r in rows]

        return await self.run(_upd)

    # ------------------------------------------------------------------ audit
    async def audit(self, row: dict[str, Any]) -> None:
        def _ins(conn):
            conn.execute(
                """INSERT INTO audit_log (at, client, method, path, query, status, request_id, body_sha256, body, note)
                   VALUES (:at, :client, :method, :path, :query, :status, :request_id, :body_sha256, :body, :note)""",
                row,
            )

        await self.run(_ins)

    async def request_owner(self, request_id: str) -> str | None:
        return await self.run(
            lambda conn: (
                row[0]
                if (
                    row := conn.execute(
                        "SELECT client FROM audit_log WHERE request_id = ? ORDER BY id LIMIT 1", (request_id,)
                    ).fetchone()
                )
                else None
            )
        )

    async def invoice_touched_by(self, invoice_id: int, client: str) -> bool:
        """True if this client requested a send approval for the invoice."""
        return await self.run(
            lambda conn: (
                conn.execute(
                    "SELECT 1 FROM approvals WHERE invoice_id = ? AND client = ? LIMIT 1", (invoice_id, client)
                ).fetchone()
                is not None
            )
        )

    # ------------------------------------------------------------------ retention
    async def purge(self, retention_days: int, now: datetime | None = None) -> dict[str, int]:
        cutoff = iso((now or utcnow()) - timedelta(days=retention_days))

        def _purge(conn):
            counts = {
                "webhook_events": conn.execute("DELETE FROM webhook_events WHERE received_at < ?", (cutoff,)).rowcount,
                "audit_log": conn.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,)).rowcount,
                # Open uploads are kept until resolved, however old.
                "uploads": conn.execute(
                    "DELETE FROM uploads WHERE created_at < ? AND done_at IS NOT NULL", (cutoff,)
                ).rowcount,
                # Never purge approvals that are still in flight.
                "approvals": conn.execute(
                    """DELETE FROM approvals WHERE created_at < ?
                       AND status NOT IN ('pending', 'approved', 'sending')""",
                    (cutoff,),
                ).rowcount,
            }
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return counts

        return await self.run(_purge)

    async def ping(self) -> bool:
        return await self.run(lambda conn: conn.execute("SELECT 1").fetchone()[0] == 1)


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data.pop("dedupe_key", None)
    return data


def _insert_approval(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO approvals (id, token_hash, invoice_id, client, request_body, summary, snapshot,
                                          content_hash, document_number, numbering_sequence,
                                          status, created_at, expires_at)
                   VALUES (:id, :token_hash, :invoice_id, :client, :request_body, :summary, :snapshot,
                           :content_hash, :document_number, :numbering_sequence,
                           'pending', :created_at, :expires_at)""",
        row,
    )
