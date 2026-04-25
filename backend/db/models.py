"""
SQLAlchemy database models
"""
import uuid
from sqlalchemy import Column, String, DateTime, Enum, Integer, Float, Boolean, Date, Text, UniqueConstraint, PrimaryKeyConstraint
from db.connection import Base
from datetime import datetime, timezone
import enum

# ==========================================
# ENUMS
# ==========================================

class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    COMPANY_ADMIN = "company_admin"
    USER = "user"


class PTOStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"


class TicketCategory(str, enum.Enum):
    BENEFITS = "benefits"
    PAYROLL = "payroll"
    WORKPLACE_ISSUE = "workplace_issue"
    GENERAL_INQUIRY = "general_inquiry"
    POLICY_QUESTION = "policy_question"
    LEAVE_RELATED = "leave_related"
    OTHER = "other"


class MeetingType(str, enum.Enum):
    IN_PERSON = "in_person"
    ONLINE = "online"
    PHONE = "phone"
    NO_MEETING = "no_meeting_needed"


class TicketStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SCHEDULED = "scheduled"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Urgency(str, enum.Enum):
    NORMAL = "normal"
    URGENT = "urgent"


# ==========================================
# MODELS
# ==========================================

class Company(Base):
    __tablename__ = "companies"
    
    name = Column(String, primary_key=True, index=True)
    domain = Column(String)  # Healthcare, Retail, etc.
    email_domain = Column(String, unique=True, index=True)
    url = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "users"
    
    email = Column(String, primary_key=True, index=True)
    password = Column(String)  # In production, use hashed passwords
    name = Column(String)
    role = Column(Enum(UserRole), default=UserRole.USER)
    company = Column(String, nullable=True)  # None for super_admin
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PTOBalance(Base):
    __tablename__ = "pto_balances"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, index=True)
    company = Column(String)
    year = Column(Integer, default=2025)
    
    total_days = Column(Float, default=15.0)  # Annual PTO allocation
    used_days = Column(Float, default=0.0)
    pending_days = Column(Float, default=0.0)  # Days in pending requests
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    __table_args__ = (
        UniqueConstraint('email', 'year', name='unique_email_year'),
    )
    
    @property
    def remaining_days(self):
        """Calculate remaining days dynamically"""
        return self.total_days - self.used_days - self.pending_days


class PTORequest(Base):
    __tablename__ = "pto_requests"
    
    id = Column(String, primary_key=True)  # UUID
    email = Column(String, index=True)
    company = Column(String, index=True)
    
    start_date = Column(Date)
    end_date = Column(Date)
    days_requested = Column(Float)  # Business days (can be 0.5 for half days)
    reason = Column(String, nullable=True)
    
    status = Column(Enum(PTOStatus), default=PTOStatus.PENDING)
    admin_notes = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)  # Admin email
    reviewed_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CompanyHoliday(Base):
    """Official company holidays (non-working days)"""
    __tablename__ = "company_holidays"
    
    id = Column(String, primary_key=True)  # UUID
    company = Column(String, index=True)
    holiday_name = Column(String)
    holiday_date = Column(Date)
    is_recurring = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CompanyBlackoutDate(Base):
    """Date ranges where PTO requests are not allowed"""
    __tablename__ = "company_blackout_dates"
    
    id = Column(String, primary_key=True)  # UUID
    company = Column(String, index=True)
    period_name = Column(String)
    start_date = Column(Date)
    end_date = Column(Date)
    reason = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class HRTicket(Base):
    """HR support tickets for employee inquiries and meeting requests"""
    __tablename__ = "hr_tickets"

    id = Column(String, primary_key=True)  # UUID
    email = Column(String, nullable=False, index=True)
    company = Column(String, nullable=False, index=True)
    
    # Request details
    subject = Column(String, nullable=False)
    description = Column(String, nullable=False)
    category = Column(Enum(TicketCategory), nullable=False)
    meeting_type = Column(Enum(MeetingType), nullable=False)
    
    preferred_date = Column(Date, nullable=True)
    preferred_time_slot = Column(String, nullable=True)
    urgency = Column(Enum(Urgency), default=Urgency.NORMAL, nullable=False)
    
    # Queue management
    status = Column(Enum(TicketStatus), default=TicketStatus.PENDING, nullable=False)
    queue_position = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    
    # Admin interaction
    assigned_to = Column(String, nullable=True)
    admin_notes = Column(String, nullable=True)
    picked_up_at = Column(DateTime, nullable=True)
    
    # Meeting details
    scheduled_datetime = Column(DateTime, nullable=True)
    meeting_link = Column(String, nullable=True)
    meeting_location = Column(String, nullable=True)
    
    # Resolution
    resolved_at = Column(DateTime, nullable=True)
    resolution_notes = Column(String, nullable=True)
    
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<HRTicket(id={self.id}, email={self.email}, subject={self.subject}, status={self.status})>"


class Conversation(Base):
    """User chat conversations"""
    __tablename__ = "conversations"
    
    id = Column(String, primary_key=True)  # UUID
    email = Column(String, nullable=False, index=True)
    company = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)  # First user message (truncated)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Message(Base):
    """Individual messages within conversations"""
    __tablename__ = "messages"
    
    id = Column(String, primary_key=True)  # UUID
    conversation_id = Column(String, nullable=False, index=True)
    
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(String, nullable=False)
    agent_type = Column(String, nullable=True)  # 'rag', 'pto', 'hr_ticket'
    
    # Metadata (stored as JSON string)
    message_metadata = Column(String, nullable=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class Task(Base):
    """Background task tracking"""
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)  # UUID
    status = Column(String, default="pending")  # pending, running, completed, failed
    message = Column(String, nullable=True)
    error = Column(String, nullable=True)

    # Task metadata
    task_type = Column(String, nullable=True)  # e.g., 'company_ingestion'
    payload = Column(String, nullable=True)  # JSON string of input args

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class IdempotencyRecord(Base):
    """Cached response for an Idempotency-Key scoped per tenant.

    Scoped by ``(key, company)`` so the same UUID cannot collide across tenants.
    Records older than 24h are purged by a daily cleanup cron.
    """
    __tablename__ = "idempotency_records"

    key = Column(String, nullable=False)
    company = Column(String, nullable=False, index=True)
    endpoint = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    response_body = Column(Text, nullable=False)  # JSON payload
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("key", "company", name="pk_idempotency_key_company"),
    )


class RefreshToken(Base):
    """Long-lived, revocable refresh token.

    Rotation-on-use: every /refresh revokes this token and issues a new one
    linked via ``rotated_from``. Re-using a revoked token revokes the entire
    chain (theft detection).
    """
    __tablename__ = "refresh_tokens"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_email = Column(String, index=True, nullable=False)
    company = Column(String, nullable=True)  # None for super_admin
    token_hash = Column(String, unique=True, nullable=False)  # hashed secret, never raw
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    rotated_from = Column(String, nullable=True)  # previous token.id in the chain