''' Application config file '''
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: SecretStr
    # Migrations run as an owner/superuser (CREATE TABLE, CREATE ROLE, GRANT);
    # database_url is the narrow app_user the API and worker run as. Unset in CI,
    # where the single DATABASE_URL already has the rights.
    alembic_database_url: SecretStr | None = None
    blob_backend: str = "local"   # local or AWS S3
    blob_local_root: str = "./_blobs"
    anthropic_api_key: SecretStr
    # Cheapest current model: picking one label from a fixed list doesn't need more.
    classify_model: str = "claude-haiku-5-5"
    # Leading characters sent to classify; the type is almost always on page one.
    classify_max_chars: int = Field(8000, ge=1)
    # Extraction has to hold several named fields plus a summary in mind at once --
    # real reading comprehension, not a single-label pick. Open to revisiting against
    # Haiku once accuracy is measured on real documents.
    extract_model: str = "claude-sonnet-5-5"
    # A field can be on any page, so extraction needs more of the document than
    # classify did.
    extract_max_chars: int = Field(20000, ge=1)
    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)
    max_attempts: int = Field(5, ge=1)
    # Push waits out downstream outages: with the 15-min backoff cap and jitter, 30
    # attempts is 2.6-5.3 h of retrying before FAILED (m10.md §3).
    push_max_attempts: int = Field(30, ge=1)
    qdrant_url: str = "http://localhost:6333"
    # embed_dim must match embed_model's output width -- a collection is created with
    # a fixed vector size, so swapping the model means a new collection, not a reuse.
    embed_model: str = "all-MiniLM-L6-v2"
    embed_dim: int = Field(384, ge=1)
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    imap_mailbox: str = "INBOX"
    imap_poll_seconds: int = Field(60, ge=1)
    # Messages fetched per poll. bulk=True keeps the batch in memory, so a backlog
    # needs a ceiling; the remainder waits for the next tick.
    imap_batch_size: int = Field(50, ge=1)
    # Cap at intake: the blob is never written, so an oversized attachment costs nothing.
    max_attachment_bytes: int = Field(25 * 1024 * 1024, ge=1)
    downstream_base_url: str  # str for now, change to HttpUrl later
    # Required: a missing secret must stop startup, never mean "accept unsigned".
    downstream_webhook_secret: SecretStr
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()