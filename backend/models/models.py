"""SQLAlchemy models for burnrate credit card analytics."""

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.models.database import Base

MAX_TAG_LENGTH = 12

def generate_uuid() -> str:
    """Generate a UUID4 string for primary keys."""
    return str(uuid4())


class Settings(Base):
    """User settings including name, DOB, and watch folder."""

    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    dob_day = Column(String(2), nullable=True)
    dob_month = Column(String(2), nullable=True)
    dob_year = Column(String(4), nullable=True)
    watch_folder = Column(String(1024), nullable=True)
    display_currency = Column(String(3), nullable=True)
    payment_reminder_last_auto_shown = Column(String(10), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DueReminderAck(Base):
    """User confirmed paid for a card's billing cycle (latest statement at ack time)."""

    __tablename__ = "due_reminder_acks"
    __table_args__ = (
        UniqueConstraint("card_id", "statement_id", name="uq_due_ack_card_statement"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    card_id = Column(String(36), ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    statement_id = Column(String(36), ForeignKey("statements.id", ondelete="CASCADE"), nullable=False)
    acknowledged_at = Column(DateTime, default=datetime.utcnow)


class Card(Base):
    """Credit card metadata."""

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("bank", "last4", name="uq_card_bank_last4"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    bank = Column(String(50), nullable=False)  # 'hdfc', 'icici', 'axis'
    last4 = Column(String(4), nullable=False)
    name = Column(String(255), nullable=True)
    template_id = Column(String(100), nullable=True)  # References CardTemplate.id from frontend
    manual_next_due_date = Column(Date, nullable=True)
    manual_next_due_amount = Column(Float, nullable=True)
    manual_due_acknowledged_for = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Statement(Base):
    """Imported credit card or bank account statement."""

    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint("file_hash", "card_last4", name="uq_statement_hash_card"),
    )

    id = Column(String(36), primary_key=True, default=generate_uuid)
    bank = Column(String(50), nullable=False)
    card_last4 = Column(String(4), nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    file_hash = Column(String(64), nullable=False)  # SHA-256
    file_path = Column(String(1024), nullable=True)
    original_upload_path = Column(String(2048), nullable=True)
    transaction_count = Column(Integer, default=0)
    total_spend = Column(Float, default=0.0)
    total_amount_due = Column(Float, nullable=True)
    credit_limit = Column(Float, nullable=True)
    source = Column(String(4), nullable=False, default="CC", server_default="CC")
    status = Column(String(20), nullable=False, default="success")
    # 1 when status is parse_error (denormalized for filtering and indexing).
    parse_failed = Column(Integer, nullable=False, default=0, server_default="0")
    status_message = Column(Text, nullable=True)
    imported_at = Column(DateTime, default=datetime.utcnow)
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    payment_due_date = Column(Date, nullable=True)
    note = Column(Text, nullable=True)
    encrypted_password = Column(Text, nullable=True)

    transactions = relationship("Transaction", back_populates="statement", cascade="all, delete-orphan")


class Transaction(Base):
    """Individual transaction from a statement."""

    __tablename__ = "transactions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    statement_id = Column(String(36), ForeignKey("statements.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    merchant = Column(String(512), nullable=False)
    txn_keyword = Column(String(10), nullable=True)
    amount = Column(Float, nullable=False)
    type = Column(String(20), nullable=False)  # 'debit' or 'credit'
    category = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    card_id = Column(String(36), ForeignKey("cards.id"), nullable=True)
    bank = Column(String(50), nullable=True)
    card_last4 = Column(String(4), nullable=True)
    source = Column(String(4), nullable=False, default="CC", server_default="CC")
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    is_manually_categorized = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow)

    statement = relationship("Statement", back_populates="transactions")
    tags = relationship("TransactionTag", back_populates="transaction", cascade="all, delete-orphan")


class TransactionTag(Base):
    """Tag attached to a transaction."""

    __tablename__ = "transaction_tags"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    transaction_id = Column(String(36), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    tag_id = Column(String(36), ForeignKey("tag_definitions.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="tags")
    tag_def = relationship("TagDefinition")


class CategoryDefinition(Base):
    """Category definition - both prebuilt and custom."""

    __tablename__ = "category_definitions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(50), nullable=False, unique=True)
    slug = Column(String(50), nullable=False, unique=True)  # lowercase, underscored key used in Transaction.category
    keywords = Column(Text, nullable=False, default="")
    color = Column(String(9), nullable=False, default="#9CA3AF")
    icon = Column(String(50), nullable=False, default="MoreHorizontal")
    is_prebuilt = Column(Integer, nullable=False, default=0)  # 1=prebuilt, 0=custom
    created_at = Column(DateTime, default=datetime.utcnow)


class TagDefinition(Base):
    """User-defined tag that can be applied to transactions."""

    __tablename__ = "tag_definitions"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(MAX_TAG_LENGTH), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProcessingLog(Base):
    """Log of statement processing attempts (success, error, duplicate)."""

    __tablename__ = "processing_logs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    file_name = Column(String(512), nullable=False)
    status = Column(String(20), nullable=False)  # success, error, duplicate
    message = Column(Text, nullable=True)
    bank = Column(String(50), nullable=True)
    transaction_count = Column(Integer, default=0)
    acknowledged = Column(Integer, default=0)  # 0=unread, 1=dismissed
    created_at = Column(DateTime, default=datetime.utcnow)


