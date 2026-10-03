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
    classify_model: str = "claude-sonnet-5"     # TODO: model cascade ? see design doc
    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)
    max_attempts: int = Field(5, ge=1)
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    downstream_base_url: str  # str for now, change to HttpUrl later
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()