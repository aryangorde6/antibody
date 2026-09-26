import sqlparse
from sqlparse import tokens as T


def test_truncate_ddl_grant_revoke_dcl():
    """TRUNCATE should be Keyword.DDL; GRANT and REVOKE should be Keyword.DCL."""
    def first_ttype(sql):
        return sqlparse.parse(sql)[0].tokens[0].ttype

    truncate_ttype = first_ttype("TRUNCATE")
    grant_ttype = first_ttype("GRANT")
    revoke_ttype = first_ttype("REVOKE")

    assert truncate_ttype == T.Keyword.DDL, (
        f"TRUNCATE ttype is {truncate_ttype}, expected {T.Keyword.DDL}"
    )
    assert grant_ttype == T.Keyword.DCL, (
        f"GRANT ttype is {grant_ttype}, expected {T.Keyword.DCL}"
    )
    assert revoke_ttype == T.Keyword.DCL, (
        f"REVOKE ttype is {revoke_ttype}, expected {T.Keyword.DCL}"
    )
