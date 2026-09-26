import sqlparse
from sqlparse import tokens as T

stmt = "DROP EXTENSION pgcrypto"
parsed = sqlparse.parse(stmt)[0]

# Find the token for EXTENSION
extension_token = None
for token in parsed.flatten():
    if token.normalized.upper() == 'EXTENSION':
        extension_token = token
        break

if extension_token is None:
    print("NO CHANGE FOUND")
elif extension_token.ttype is T.Keyword:
    print(f"NO CHANGE FOUND")
else:
    print(f"DIFFERENCE FOUND: {extension_token.ttype} vs {T.Keyword}")
