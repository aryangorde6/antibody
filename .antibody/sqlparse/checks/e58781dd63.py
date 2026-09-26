import sqlparse
from sqlparse import tokens as T

# Parse a SQL statement with INDICATOR keyword
sql = "SELECT INDICATOR FROM foo"
parsed = sqlparse.parse(sql)[0]

# Find the token for INDICATOR
indicator_token = None
for token in parsed.flatten():
    if token.normalized == 'INDICATOR':
        indicator_token = token
        break

if indicator_token is not None and indicator_token.ttype is T.Keyword:
    print("NO CHANGE FOUND")
else:
    # With the bug, INDICATOR was not in keywords dict (only INDITCATOR was)
    # so it would be treated as a Name/Identifier, not a Keyword
    actual_ttype = indicator_token.ttype if indicator_token else None
    print(f"DIFFERENCE FOUND: {actual_ttype} vs {T.Keyword}")
