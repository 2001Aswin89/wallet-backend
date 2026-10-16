import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
