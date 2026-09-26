import sqlparse
from sqlparse import tokens as T

def get_token_ttype(sql):
    parsed = sqlparse.parse(sql)[0]
    return parsed.tokens[0].ttype

truncate_ttype = get_token_ttype("TRUNCATE")
grant_ttype = get_token_ttype("GRANT")
revoke_ttype = get_token_ttype("REVOKE")

expected_truncate = T.Keyword.DDL
expected_dcl = T.Keyword.DCL

if truncate_ttype == expected_truncate and grant_ttype == expected_dcl and revoke_ttype == expected_dcl:
    print("NO CHANGE FOUND")
else:
    parts = []
    if truncate_ttype != expected_truncate:
        parts.append(f"TRUNCATE={truncate_ttype} (expected {expected_truncate})")
    if grant_ttype != expected_dcl:
        parts.append(f"GRANT={grant_ttype} (expected {expected_dcl})")
    if revoke_ttype != expected_dcl:
        parts.append(f"REVOKE={revoke_ttype} (expected {expected_dcl})")
    print(f"DIFFERENCE FOUND: {'; '.join(parts)} vs correct DDL/DCL classification")
