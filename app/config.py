"""
Server-level config only.
API keys are now stored per-user in the database (app/database.py).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    RESEARCH_RATE_LIMIT_PER_MINUTE = int(os.getenv("RESEARCH_RATE_LIMIT_PER_MINUTE", "5"))
    INGEST_RATE_LIMIT_PER_HOUR     = int(os.getenv("INGEST_RATE_LIMIT_PER_HOUR", "3"))


settings = Settings()
