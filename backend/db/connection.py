"""
Database connection and session management
"""
import logging
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

def get_database_url():
    """Auto-detect which database to use based on availability.

    In production (ENVIRONMENT=production), PostgreSQL is required — raises
    RuntimeError if unreachable instead of silently falling back to an
    ephemeral SQLite file (which would lose all writes on container restart).
    """
    postgres_url = "postgresql://postgres:MLOpsgroup%409@127.0.0.1:5432/frontshiftai"
    sqlite_url = "sqlite:///./frontshiftai.db"

    try:
        test_engine = create_engine(postgres_url, poolclass=NullPool)
        with test_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        test_engine.dispose()
        print("✅ Using PostgreSQL (Cloud SQL)")
        return postgres_url
    except Exception as e:
        if os.getenv("ENVIRONMENT") == "production":
            raise RuntimeError(
                f"PostgreSQL required in production but unavailable: {e}"
            ) from e
        logger.warning("PostgreSQL unavailable, falling back to SQLite (dev/test only)")
        return sqlite_url

# Allow override via environment variable, otherwise auto-detect
DATABASE_URL = os.getenv("DATABASE_URL") or get_database_url()

# Configure engine based on database type
if DATABASE_URL.startswith("postgresql"):
    # PostgreSQL configuration for Cloud SQL
    engine = create_engine(
        DATABASE_URL,
        poolclass=NullPool,  # Cloud Run manages connections
        pool_pre_ping=True,   # Verify connections before use
        echo=False
    )
else:
    # SQLite configuration (local development)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize database with all tables"""
    from db import models  # Import models to register them
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created")