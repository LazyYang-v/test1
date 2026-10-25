"""Migration SQL files reference passwords via {{env:NAME}} tokens.

The runner substitutes a safely quoted literal at apply time, so no
credential ever lands in a migration file or the repository.
"""

import pytest

from agentforge_core.migrations import MigrationError, substitute_env


def test_substitutes_token_with_quoted_literal():
    sql = substitute_env("PASSWORD {{env:ROLE_PW}};", {"ROLE_PW": "s3cret"})
    assert sql == "PASSWORD 's3cret';"


def test_escapes_single_quotes_in_value():
    sql = substitute_env("PASSWORD {{env:ROLE_PW}};", {"ROLE_PW": "o'brien"})
    assert sql == "PASSWORD 'o''brien';"


def test_missing_env_variable_raises_with_variable_name():
    with pytest.raises(MigrationError, match="ROLE_PW"):
        substitute_env("PASSWORD {{env:ROLE_PW}};", {})


def test_repeated_tokens_are_all_substituted():
    sql = substitute_env("{{env:A}} {{env:A}}", {"A": "x"})
    assert sql == "'x' 'x'"
