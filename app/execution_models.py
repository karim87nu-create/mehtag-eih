"""Additive tables: no changes to conversation, request or persistence schemas."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from .db import Base

class VerifiedChannel(Base):
    __tablename__ = 'verified_channels'
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey('businesses.id'), nullable=False, unique=True)
    endpoint = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    consent_basis = Column(Text, nullable=False)
    status = Column(String, nullable=False, default='VERIFIED')
    verified_at = Column(DateTime, default=datetime.utcnow)

class OutboundJob(Base):
    __tablename__ = 'outbound_jobs'
    __table_args__ = (UniqueConstraint('request_id', 'business_id'),)
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey('requests.id'), nullable=False)
    business_id = Column(Integer, ForeignKey('businesses.id'), nullable=False)
    attempt_id = Column(Integer, ForeignKey('reach_attempts.id'), nullable=False)
    delivery_id = Column(Integer, ForeignKey('transport_deliveries.id'), nullable=False)
    channel_id = Column(Integer, ForeignKey('verified_channels.id'), nullable=False)
    endpoint = Column(Text, nullable=False)
    idempotency_key = Column(String, unique=True, nullable=False)
    status = Column(String, nullable=False, default='QUEUED')
    provider_ref = Column(String)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

class ExecutionNotice(Base):
    __tablename__ = 'execution_notices'
    __table_args__ = (UniqueConstraint('thread_id', 'event_key'),)
    id = Column(Integer, primary_key=True)
    thread_id = Column(String, ForeignKey('conversation_threads.id'), nullable=False)
    request_id = Column(Integer, ForeignKey('requests.id'), nullable=False)
    event_key = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class IncomingReceipt(Base):
    __tablename__ = 'incoming_receipts'
    __table_args__ = (UniqueConstraint('channel_id', 'event_id'),)
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey('outbound_jobs.id'), nullable=False, unique=True)
    channel_id = Column(Integer, ForeignKey('verified_channels.id'), nullable=False)
    event_id = Column(String, nullable=False)
    payload_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class DiscoveryCache(Base):
    __tablename__ = 'discovery_cache'
    key = Column(String, primary_key=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
