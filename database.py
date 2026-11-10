import os
import sqlite3
import hashlib
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_FILE = "brainbuddy.db"
DATABASE_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# -------------------------------------------------------------
# DATABASE MODELS
# -------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    materials = relationship("StudyMaterial", back_populates="user", cascade="all, delete-orphan")
    performance_records = relationship("TopicPerformance", back_populates="user", cascade="all, delete-orphan")
    quiz_attempts = relationship("QuizAttempt", back_populates="user", cascade="all, delete-orphan")


class StudyMaterial(Base):
    __tablename__ = "study_materials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    filename = Column(String(200), nullable=False)
    extracted_text = Column(Text, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="materials")
    quiz_attempts = relationship("QuizAttempt", back_populates="material", cascade="all, delete-orphan")


class TopicPerformance(Base):
    __tablename__ = "topic_performance"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    topic = Column(String(150), nullable=False)
    total_questions = Column(Integer, default=0)
    correct_answers = Column(Integer, default=0)
    score = Column(Float, default=0.0)
    status = Column(String(50), default="Needs Revision")
    last_updated = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="performance_records")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("study_materials.id"), nullable=False)
    score = Column(Float, nullable=False)
    total_correct = Column(Integer, nullable=False)
    total_questions = Column(Integer, nullable=False)
    attempted_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="quiz_attempts")
    material = relationship("StudyMaterial", back_populates="quiz_attempts")


# -------------------------------------------------------------
# SAFE AUTOMATIC MIGRATION (PRESERVES EXISTING DATA)
# -------------------------------------------------------------
def migrate_existing_database():
    """Inspects existing SQLite database and safely adds missing columns without deleting records."""
    if not os.path.exists(DB_FILE):
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Check existing columns in the 'users' table
        cursor.execute("PRAGMA table_info(users);")
        columns = [row[1] for row in cursor.fetchall()]

        # If 'password_hash' column is missing, add it dynamically
        if 'password_hash' not in columns and len(columns) > 0:
            # Set a default hashed password ('123456') for pre-existing demo accounts
            default_salted = "brainbuddy_salt_123456"
            default_hash = hashlib.sha256(default_salted.encode()).hexdigest()

            cursor.execute(f"ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) DEFAULT '{default_hash}';")
            conn.commit()
            print("[Database Safe Migration] Successfully added 'password_hash' column to existing users table.")

        conn.close()
    except Exception as e:
        print(f"[Database Migration Warning] {e}")


# Run migration first, then ensure all tables are created
migrate_existing_database()
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()