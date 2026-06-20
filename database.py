from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "postgresql://neondb_owner:npg_eEjU2qpkVic5@ep-steep-rain-ah8iggp7-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models import Survey, Question, SurveyResponse  # noqa
    Base.metadata.create_all(bind=engine)

    # Migrate: add new columns to responses table if missing
    insp = inspect(engine)
    if "responses" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("responses")]
        migrations = {
            "survey_id": "ALTER TABLE responses ADD COLUMN survey_id INTEGER",
            "first_name": "ALTER TABLE responses ADD COLUMN first_name VARCHAR(120)",
            "last_name": "ALTER TABLE responses ADD COLUMN last_name VARCHAR(120)",
            "contact": "ALTER TABLE responses ADD COLUMN contact VARCHAR(200)",
        }
        with engine.connect() as conn:
            for col, ddl in migrations.items():
                if col not in cols:
                    conn.execute(text(ddl))
            conn.commit()

    # Migrate: тип вопроса (выбор / открытый текстовый ответ)
    if "survey_questions" in insp.get_table_names():
        q_cols = [c["name"] for c in insp.get_columns("survey_questions")]
        if "qtype" not in q_cols:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE survey_questions ADD COLUMN qtype VARCHAR(20) DEFAULT 'choice'"
                ))
                conn.execute(text(
                    "UPDATE survey_questions SET qtype = 'choice' WHERE qtype IS NULL"
                ))
                conn.commit()
