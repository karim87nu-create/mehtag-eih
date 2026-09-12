from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from .db import Base

class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        UniqueConstraint("customer_ref", "source_turn_id", name="uq_requests_customer_source_turn"),
    )
    id = Column(Integer, primary_key=True)
    # Rows without an owner are legacy/quarantined and are never customer-visible.
    customer_ref = Column(String(36), nullable=True, index=True)
    locale = Column(String(20), nullable=True)
    region = Column(String(16), nullable=True)
    currency = Column(String(8), nullable=True)
    # The chat turn that authorized this request.  NULL keeps legacy/non-chat
    # entry points backwards compatible; a customer turn can create it once.
    source_turn_id = Column(String(36), nullable=True)
    raw_text = Column(Text, nullable=False)
    item = Column(String, nullable=True)
    area = Column(String, nullable=True)
    budget = Column(Float, nullable=True)
    deadline = Column(String, nullable=True)
    status = Column(String, default="NEW")
    created_at = Column(DateTime, default=datetime.utcnow)
    offers = relationship("Offer", back_populates="request", cascade="all, delete-orphan")
    events = relationship("Event", back_populates="request", cascade="all, delete-orphan")

class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True)
    external_id = Column(String, nullable=True, unique=True)
    name = Column(String, nullable=False)
    website = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    source = Column(String, default="discovery")
    created_at = Column(DateTime, default=datetime.utcnow)

class MerchantLink(Base):
    __tablename__ = "merchant_links"
    id = Column(Integer, primary_key=True)
    token = Column(String, unique=True, index=True, nullable=False)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    status = Column(String, default="CREATED")
    created_at = Column(DateTime, default=datetime.utcnow)

class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("request_id", "business_id", name="uq_offers_request_business"),
    )
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    price = Column(Float, nullable=False)
    eta = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String, default="VALID")
    created_at = Column(DateTime, default=datetime.utcnow)
    request = relationship("Request", back_populates="offers")

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    event_type = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    request = relationship("Request", back_populates="events")


class ExecutionCase(Base):
    __tablename__ = "execution_cases"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False, unique=True)
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=False)
    status = Column(String, default="AWAITING_PAYMENT")
    payment_status = Column(String, default="NOT_STARTED")
    outcome_status = Column(String, default="OPEN")
    expected_at = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class CaseEvent(Base):
    __tablename__ = "case_events"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("execution_cases.id"), nullable=False)
    event_type = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExternalCase(Base):
    __tablename__ = "external_cases"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    locale = Column(String(20), nullable=True)
    region = Column(String(16), nullable=True)
    currency = Column(String(8), nullable=True)
    source_text = Column(Text, nullable=False)
    title = Column(String, nullable=False)
    expected_at = Column(String, nullable=True)
    status = Column(String, default="FOLLOWING")
    outcome_status = Column(String, default="OPEN")
    created_at = Column(DateTime, default=datetime.utcnow)

class MemoryFact(Base):
    __tablename__ = "memory_facts"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    memory_type = Column(String, nullable=False)  # customer / market
    key = Column(String, nullable=False)
    value = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)
    source_id = Column(Integer, nullable=True)
    confidence = Column(Float, default=1.0)
    created_at = Column(DateTime, default=datetime.utcnow)


class DetectedTransaction(Base):
    __tablename__ = "detected_transactions"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    locale = Column(String(20), nullable=True)
    region = Column(String(16), nullable=True)
    currency = Column(String(8), nullable=True)
    source = Column(String, nullable=False)  # email/share/api/device-later
    source_ref = Column(String, nullable=True)
    raw_text = Column(Text, nullable=False)
    title = Column(String, nullable=False)
    transaction_type = Column(String, nullable=True)
    expected_at = Column(String, nullable=True)
    suggested_action = Column(String, nullable=True)
    status = Column(String, default="SUGGESTED")  # SUGGESTED/ACCEPTED/DISMISSED
    created_at = Column(DateTime, default=datetime.utcnow)


class TransactionEvent(Base):
    __tablename__ = "transaction_events"
    id = Column(Integer, primary_key=True)
    detected_transaction_id = Column(Integer, ForeignKey("detected_transactions.id"), nullable=False)
    event_type = Column(String, nullable=False)
    raw_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class FollowupRule(Base):
    __tablename__ = "followup_rules"
    id = Column(Integer, primary_key=True)
    transaction_type = Column(String, nullable=False)
    trigger_event = Column(String, nullable=False)
    action = Column(String, nullable=False)
    delay_minutes = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)

class FollowupTask(Base):
    __tablename__ = "followup_tasks"
    id = Column(Integer, primary_key=True)
    external_case_id = Column(Integer, ForeignKey("external_cases.id"), nullable=False)
    trigger_event = Column(String, nullable=False)
    action = Column(String, nullable=False)
    due_at = Column(DateTime, nullable=False)
    status = Column(String, default="PENDING")
    created_at = Column(DateTime, default=datetime.utcnow)


class LearnedPreference(Base):
    __tablename__ = "learned_preferences"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    preference_key = Column(String, nullable=False)
    preference_value = Column(Text, nullable=False)
    evidence_count = Column(Integer, default=1)
    confidence = Column(Float, default=0.0)
    status = Column(String, default="OBSERVED")  # OBSERVED/SUGGESTIBLE/CONFIRMED
    created_at = Column(DateTime, default=datetime.utcnow)


class BusinessPerformance(Base):
    __tablename__ = "business_performance"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, unique=True)
    completed = Column(Integer, default=0)
    verified_ok = Column(Integer, default=0)
    issues = Column(Integer, default=0)
    cancellations = Column(Integer, default=0)
    late = Column(Integer, default=0)
    price_changes = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class OfferAmendment(Base):
    __tablename__ = "offer_amendments"
    id = Column(Integer, primary_key=True)
    offer_id = Column(Integer, ForeignKey("offers.id"), nullable=False)
    new_price = Column(Float, nullable=True)
    new_eta = Column(String, nullable=True)
    reason = Column(Text, nullable=False)
    status = Column(String, default="PENDING")  # PENDING/APPROVED/REJECTED
    created_at = Column(DateTime, default=datetime.utcnow)

class IssueRecord(Base):
    __tablename__ = "issue_records"
    id = Column(Integer, primary_key=True)
    execution_case_id = Column(Integer, ForeignKey("execution_cases.id"), nullable=False)
    issue_text = Column(Text, nullable=False)
    status = Column(String, default="OPEN")  # OPEN/RESOLVED_PENDING_CONFIRMATION/VERIFIED_RESOLVED
    resolution_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class CustomerRule(Base):
    __tablename__ = "customer_rules"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    rule_key = Column(String, nullable=False)
    rule_value = Column(Text, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CapabilitySignal(Base):
    __tablename__ = "capability_signals"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    capability = Column(String, nullable=False)
    area = Column(String, nullable=True)
    accepted_count = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

class ReachAttempt(Base):
    __tablename__ = "reach_attempts"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    channel = Column(String, nullable=False)
    endpoint = Column(String, nullable=True)
    status = Column(String, default="PENDING")  # SENT/OPENED/RESPONDED/SKIPPED/FAILED
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BusinessActivation(Base):
    __tablename__ = "business_activations"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, unique=True)
    status = Column(String, default="REQUEST_ONLY")  # REQUEST_ONLY/DIRECT
    preferred_channel = Column(String, nullable=True)
    endpoint = Column(String, nullable=True)
    consent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class IntegrationEndpoint(Base):
    __tablename__ = "integration_endpoints"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True)
    kind = Column(String, nullable=False)  # WEBHOOK/API/EMAIL/WHATSAPP_OPTED_IN
    endpoint = Column(String, nullable=False)
    status = Column(String, default="TEST")
    consent_basis = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class TransportDelivery(Base):
    __tablename__ = "transport_deliveries"
    id = Column(Integer, primary_key=True)
    reach_attempt_id = Column(Integer, ForeignKey("reach_attempts.id"), nullable=False)
    transport = Column(String, nullable=False)
    status = Column(String, default="QUEUED")  # QUEUED/SENT/FAILED
    provider_ref = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    id = Column(Integer, primary_key=True)
    execution_case_id = Column(Integer, ForeignKey("execution_cases.id"), nullable=False)
    amount = Column(Float, nullable=False)
    service_fee = Column(Float, nullable=False)
    # Callers set this from the owning request's locale. ``XXX`` is the neutral
    # ISO fallback; a missing value must never silently become Egyptian pounds.
    currency = Column(String(8), nullable=False, default="XXX")
    provider = Column(String, default="SIMULATION")
    provider_ref = Column(String, nullable=True)
    status = Column(String, default="CREATED")
    created_at = Column(DateTime, default=datetime.utcnow)


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    id = Column(Integer, primary_key=True)
    subject_type = Column(String, nullable=False)  # CUSTOMER/BUSINESS
    subject_ref = Column(String, nullable=False)
    consent_type = Column(String, nullable=False)
    status = Column(String, default="GRANTED")
    source = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AuditRecord(Base):
    __tablename__ = "audit_records"
    id = Column(Integer, primary_key=True)
    actor = Column(String, default="SYSTEM")
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MobileSourceEvent(Base):
    __tablename__ = "mobile_source_events"
    id = Column(Integer, primary_key=True)
    customer_ref = Column(String(36), nullable=True, index=True)
    locale = Column(String(20), nullable=True)
    region = Column(String(16), nullable=True)
    currency = Column(String(8), nullable=True)
    source = Column(String, nullable=False)   # SHARE / NOTIFICATION / EMAIL_CONNECTOR / MANUAL
    package_name = Column(String, nullable=True)
    title = Column(Text, nullable=True)
    body = Column(Text, nullable=False)
    event_hash = Column(String, nullable=False, unique=True)
    status = Column(String, default="RECEIVED")
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationThread(Base):
    """A durable customer conversation, independent from any single request/case."""
    __tablename__ = "conversation_threads"
    id = Column(String, primary_key=True)
    customer_ref = Column(String(36), nullable=False, index=True)
    locale = Column(String, nullable=False, default="ar-EG")
    language = Column(String, nullable=False, default="ar")
    currency = Column(String, nullable=False, default="EGP")
    region = Column(String, nullable=False, default="EG")
    status = Column(String, nullable=False, default="ACTIVE")
    provider = Column(String, nullable=True)
    degraded_mode = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id = Column(Integer, primary_key=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    role = Column(String, nullable=False)  # USER / ASSISTANT / SYSTEM
    content = Column(Text, nullable=False)
    intent = Column(String, nullable=True)
    style_metadata = Column(Text, nullable=True)  # JSON: language, tone, mood, urgency, formality
    provider_metadata = Column(Text, nullable=True)  # JSON: model/provider/degraded
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationTurn(Base):
    """Idempotency/lease record for one client-submitted chat turn."""
    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("customer_ref", "client_turn_id", name="uq_conversation_turn_customer_client"),
        UniqueConstraint("thread_id", "client_turn_id", name="uq_conversation_turn_thread_client"),
    )
    id = Column(Integer, primary_key=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    customer_ref = Column(String(36), nullable=False, index=True)
    client_turn_id = Column(String(36), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    status = Column(String, nullable=False, default="PROCESSING")  # PROCESSING/COMPLETED/FAILED
    lease_token = Column(String(36), nullable=False)
    attempt_count = Column(Integer, nullable=False, default=1)
    user_message_id = Column(Integer, ForeignKey("conversation_messages.id"), nullable=True)
    response_envelope = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class ConversationCaseLink(Base):
    __tablename__ = "conversation_case_links"
    id = Column(Integer, primary_key=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    case_type = Column(String, nullable=False)  # REQUEST / EXECUTION / EXTERNAL
    case_id = Column(Integer, nullable=False)
    relationship = Column(String, nullable=False, default="PRIMARY")
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationAction(Base):
    """Audit trail for proposed, blocked, and executed business actions."""
    __tablename__ = "conversation_actions"
    id = Column(Integer, primary_key=True)
    thread_id = Column(String, ForeignKey("conversation_threads.id"), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("conversation_messages.id"), nullable=True)
    action_type = Column(String, nullable=False)
    status = Column(String, nullable=False)  # PROPOSED / BLOCKED / EXECUTED / FAILED
    reason = Column(Text, nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
