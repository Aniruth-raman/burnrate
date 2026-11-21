"""Regression tests for core-only behaviour."""

from datetime import date
from uuid import uuid4

import pytest

from backend.models.models import (
    Card,
    Statement,
    TagDefinition,
    Transaction,
    TransactionTag,
)


def _statement(db, **overrides):
    stmt = Statement(
        bank=overrides.get("bank", "testbank"),
        card_last4=overrides.get("card_last4"),
        period_start=overrides.get("period_start", date(1999, 1, 1)),
        period_end=overrides.get("period_end", date(1999, 1, 31)),
        file_hash=overrides.get("file_hash", uuid4().hex),
        source=overrides.get("source", "CC"),
        status="success",
        transaction_count=1,
        total_spend=overrides.get("total_spend", 0.0),
        currency="INR",
    )
    db.add(stmt)
    db.flush()
    return stmt


def _txn(db, stmt, **overrides):
    tx = Transaction(
        statement_id=stmt.id,
        date=overrides.get("date", date(1999, 1, 15)),
        merchant=overrides.get("merchant", "Test Merchant"),
        amount=overrides.get("amount", 100.0),
        type=overrides.get("type", "debit"),
        category=overrides.get("category", "other"),
        bank=overrides.get("bank", stmt.bank),
        card_last4=overrides.get("card_last4", stmt.card_last4),
        source=overrides.get("source", stmt.source),
        currency="INR",
    )
    db.add(tx)
    db.flush()
    return tx


class TestAnalyticsRegressions:
    def test_tag_filter_does_not_crash(self, api_client, db):
        stmt = _statement(db)
        _txn(db, stmt, merchant="TAG TEST", category="food")
        tag = TagDefinition(name="regressiontag")
        db.add(tag)
        db.flush()
        db.add(TransactionTag(transaction_id=_txn(db, stmt, merchant="TAG TEST 2").id, tag_id=tag.id))
        db.commit()
        try:
            resp = api_client.get(
                "/api/analytics/summary",
                params={"tags": "regressiontag", "from": "1999-01-01", "to": "1999-01-31"},
            )
            assert resp.status_code == 200, resp.text
        finally:
            db.delete(stmt)
            db.delete(tag)
            db.commit()

    def test_refunds_reduce_category_and_merchant_totals(self, api_client, db):
        stmt = _statement(db)
        _txn(db, stmt, merchant="REFUND TEST", amount=1000.0, category="groceries")
        _txn(
            db, stmt, merchant="REFUND TEST", amount=400.0,
            type="credit", category="groceries",
        )
        db.commit()
        params = {"from": "1999-01-01", "to": "1999-01-31"}
        try:
            summary = api_client.get("/api/analytics/summary", params=params).json()
            assert summary["totalSpend"] == 600.0

            categories = api_client.get("/api/analytics/categories", params=params).json()
            groceries = next(c for c in categories["breakdown"] if c["category"] == "groceries")
            assert groceries["amount"] == 600.0

            merchants = api_client.get("/api/analytics/merchants", params=params).json()
            merchant = next(m for m in merchants["merchants"] if m["merchant"] == "REFUND TEST")
            assert merchant["amount"] == 600.0
        finally:
            db.delete(stmt)
            db.commit()

    def test_statement_periods_only_use_their_own_transactions(self, api_client, db):
        bank_stmt = _statement(
            db, bank="periodbank", card_last4=None, source="BANK", total_spend=100.0
        )
        card_stmt = _statement(
            db, bank="periodbank", card_last4="7777", source="CC", total_spend=900.0
        )
        _txn(db, bank_stmt, amount=100.0, source="BANK", card_last4=None)
        _txn(db, card_stmt, amount=900.0, source="CC", card_last4="7777")
        db.commit()
        try:
            resp = api_client.get(
                "/api/analytics/statement-periods",
                params={"from": "1999-01-01", "to": "1999-01-31"},
            )
            assert resp.status_code == 200
            rows = [
                r for r in resp.json()["periods"]
                if r["bank"] == "periodbank" and r["periodStart"] == "1999-01-01"
            ]
            by_card = {r["cardLast4"]: r["totalSpend"] for r in rows}
            assert by_card[None] == 100.0
            assert by_card["7777"] == 900.0
        finally:
            db.delete(bank_stmt)
            db.delete(card_stmt)
            db.commit()


class TestTransactionFilterRegressions:
    def test_multiple_categories_filter(self, api_client, db):
        stmt = _statement(db)
        _txn(db, stmt, merchant="MULTI CAT A", category="food")
        _txn(db, stmt, merchant="MULTI CAT B", category="travel")
        db.commit()
        try:
            resp = api_client.get(
                "/api/transactions",
                params={"categories": "food,travel", "search": "MULTI CAT"},
            )
            assert resp.status_code == 200
            assert resp.json()["total"] == 2
        finally:
            db.delete(stmt)
            db.commit()


class TestSettingsValidation:
    def test_blank_card_is_rejected(self, api_client):
        before = api_client.get("/api/settings").json()
        resp = api_client.put(
            "/api/settings",
            json={"name": "Test", "cards": [{"bank": "", "last4": ""}]},
        )
        assert resp.status_code == 400
        after = api_client.get("/api/settings").json()
        assert len(after["cards"]) == len(before["cards"])


class TestCategoryRegressions:
    def test_rename_moves_transactions_and_delete_refuses_when_in_use(self, api_client, db):
        created = api_client.post(
            "/api/categories/custom",
            json={"name": "Old Cat", "keywords": "", "color": "#123456"},
        )
        assert created.status_code == 200
        category = created.json()

        stmt = _statement(db)
        tx = _txn(db, stmt, category=category["slug"])
        db.commit()
        try:
            renamed = api_client.put(
                f"/api/categories/{category['id']}", json={"name": "New Cat"}
            )
            assert renamed.status_code == 200
            db.refresh(tx)
            assert tx.category == "new_cat"

            refused = api_client.delete(f"/api/categories/custom/{category['id']}")
            assert refused.status_code == 400

            db.delete(stmt)
            db.commit()
            deleted = api_client.delete(f"/api/categories/custom/{category['id']}")
            assert deleted.status_code == 200
        finally:
            if db.query(Statement).filter(Statement.id == stmt.id).first():
                db.delete(stmt)
                db.commit()


class TestReparseAll:
    def test_reparse_all_processes_existing_statements(self, api_client):
        from pathlib import Path

        csv_path = Path(__file__).parent / "fixtures" / "hdfc_bank_min.csv"
        with open(csv_path, "rb") as handle:
            upload = api_client.post(
                "/api/statements/upload",
                files={"file": ("hdfc_bank_min.csv", handle, "text/csv")},
                data={"source": "BANK"},
            )
        assert upload.status_code == 200
        assert upload.json()["status"] in ("success", "duplicate")

        resp = api_client.post("/api/statements/reparse-all", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] >= 1


class TestReparseRegressions:
    def test_reparse_replaces_a_password_needed_row(self, api_client, db):
        from pathlib import Path

        csv_path = Path(__file__).parent / "fixtures" / "hdfc_bank_min.csv"
        with open(csv_path, "rb") as handle:
            upload = api_client.post(
                "/api/statements/upload",
                files={"file": ("hdfc_bank_min.csv", handle, "text/csv")},
                data={"source": "BANK"},
            )
        assert upload.status_code == 200

        stmt = (
            db.query(Statement)
            .filter(Statement.status == "success", Statement.source == "BANK")
            .order_by(Statement.imported_at.desc())
            .first()
        )
        assert stmt is not None
        file_hash = stmt.file_hash
        stmt.status = "password_needed"
        stmt.card_last4 = None
        stmt.note = "keep this note"
        db.commit()
        try:
            resp = api_client.post(f"/api/statements/{stmt.id}/reparse")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "success"

            db.expire_all()
            rows = db.query(Statement).filter(Statement.file_hash == file_hash).all()
            assert len(rows) == 1
            assert rows[0].status == "success"
            assert rows[0].note == "keep this note"
        finally:
            db.expire_all()
            for row in db.query(Statement).filter(Statement.file_hash == file_hash).all():
                db.delete(row)
            db.commit()


class TestPaymentReminderRegressions:
    def test_manual_due_date_still_reminds_after_statement_ack(self, api_client, db):
        from datetime import timedelta

        today = date.today()
        last4 = uuid4().hex[:4].upper()
        card = Card(bank="reminderbank", last4=last4)
        db.add(card)
        db.flush()
        stmt = Statement(
            bank="reminderbank",
            card_last4=last4,
            period_start=today - timedelta(days=30),
            period_end=today,
            file_hash=uuid4().hex,
            source="CC",
            status="success",
            payment_due_date=today + timedelta(days=2),
            total_amount_due=500.0,
        )
        db.add(stmt)
        db.commit()
        try:
            ack = api_client.post("/api/due-reminders/ack", json={"card_id": card.id})
            assert ack.status_code == 200

            patched = api_client.patch(
                f"/api/cards/{card.id}",
                json={"manual_next_due_date": (today + timedelta(days=4)).isoformat()},
            )
            assert patched.status_code == 200

            items = api_client.get(
                "/api/due-reminders", params={"local_date": today.isoformat()}
            ).json()["items"]
            assert any(item["cardId"] == card.id for item in items)
        finally:
            db.delete(stmt)
            db.delete(card)
            db.commit()
