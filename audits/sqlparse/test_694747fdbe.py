import sqlparse
from sqlparse import tokens as T


def test_attach_detach_are_keywords():
    # ATTACH and DETACH should be recognized as Keyword tokens in PostgreSQL dialect
    for word, sql in [
        ("DETACH", "ALTER TABLE atable DETACH PARTITION atable_p00"),
        ("ATTACH", "ALTER INDEX atable_acolumn_idx ATTACH PARTITION atable_p01_acolumn_idx"),
    ]:
        parsed = sqlparse.parse(sql)[0]
        found_type = None
        for token in parsed.flatten():
            if token.normalized.upper() == word:
                found_type = token.ttype
                break
        assert found_type == T.Keyword, (
            f"Expected {word} to have token type {T.Keyword}, got {found_type}"
        )
