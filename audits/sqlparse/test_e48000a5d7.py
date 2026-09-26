import sqlparse
from sqlparse import tokens as T


def test_extension_tokenized_as_keyword():
    """EXTENSION should be tokenized as a Keyword, not part of an Identifier."""
    stmt = "DROP EXTENSION pgcrypto"
    parsed = sqlparse.parse(stmt)[0]

    extension_token = None
    for token in parsed.flatten():
        if token.normalized.upper() == 'EXTENSION':
            extension_token = token
            break

    assert extension_token is not None, "EXTENSION token not found in parsed statement"
    assert extension_token.ttype is T.Keyword, (
        f"Expected EXTENSION to have ttype {T.Keyword}, got {extension_token.ttype}"
    )
