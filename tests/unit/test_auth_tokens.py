"""Offline tests for public JWT guarantees."""

from datetime import UTC, datetime

import jwt

import pytest
from pydantic import ValidationError

from agentforge_api.auth import create_access_token
from agentforge_api.config import Settings


def test_access_token_is_hs256_and_never_lives_longer_than_fifteen_minutes():
    secret = "offline-signing-secret-that-is-at-least-32-bytes"
    token, expires_in = create_access_token(
        user_id="0192a3b4-c5d6-7e8f-9012-3456789abcde",
        token_version=7,
        secret=secret,
        ttl_seconds=3600,
    )

    claims = jwt.decode(token, secret, algorithms=["HS256"])
    assert claims["token_version"] == 7
    assert claims["exp"] - claims["iat"] == 900
    assert expires_in == 900
    assert datetime.fromtimestamp(claims["exp"], UTC) > datetime.now(UTC)


def test_api_settings_reject_database_superuser_dsn():
    with pytest.raises(ValidationError, match="agentforge_api role"):
        Settings(api_db_dsn="postgresql://postgres:secret@localhost:5432/agentforge")
