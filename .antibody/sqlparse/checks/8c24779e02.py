import sqlparse

# Test that JSON operators -> and ->> are properly grouped as identifiers
# Without the fix, foo->'age' wouldn't be grouped as a single identifier,
# causing wrong indentation when reindenting SQL

sql = "SELECT foo->'name', foo->>'age' FROM users;"

parsed = sqlparse.parse(sql)[0]
formatted = sqlparse.format(sql, reindent=True)

# With the fix, the tokens in the select list should be properly grouped
# and the formatted output shouldn't have extra indentation on subsequent columns
lines = formatted.splitlines()

# The select should produce aligned columns - all at same indent level
# Collect lines with foo
foo_lines = [l for l in lines if "foo" in l]

if len(foo_lines) >= 2:
    indents = [len(l) - len(l.lstrip()) for l in foo_lines]
    if len(set(indents)) > 1:
        print(f"DIFFERENCE FOUND: indents={indents} vs all-equal")
    else:
        print("NO CHANGE FOUND")
else:
    # Check another way - look at how the tokens are grouped
    # The statement with -> should have foo->'name' as one Identifier
    stmt = sqlparse.parse("SELECT foo->'name' FROM t;")[0]
    tokens_flat = list(stmt.flatten())
    # With the fix, -> and the string after are grouped with foo
    # Let's count Identifier tokens in the parsed select
    from sqlparse import sql as sqltypes
    identifiers = [t for t in stmt.tokens if isinstance(t, sqltypes.IdentifierList)]
    if identifiers:
        id_list = identifiers[0]
        ids = list(id_list.get_identifiers())
        print(f"DIFFERENCE FOUND: {len(ids)} identifiers found (bug may cause wrong grouping)")
    else:
        print("NO CHANGE FOUND")
