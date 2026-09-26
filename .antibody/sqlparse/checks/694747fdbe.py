import sqlparse
from sqlparse import tokens as T

# Parse a PostgreSQL statement using ATTACH and DETACH keywords
sql = "ALTER TABLE atable DETACH PARTITION atable_p00"
parsed = sqlparse.parse(sql)[0]

# Find token types for ATTACH and DETACH
detach_type = None
attach_type = None

for token in parsed.flatten():
    if token.normalized.upper() == 'DETACH':
        detach_type = token.ttype
    if token.normalized.upper() == 'ATTACH':
        attach_type = token.ttype

# Also test ATTACH
sql2 = "ALTER INDEX atable_acolumn_idx ATTACH PARTITION atable_p01_acolumn_idx"
parsed2 = sqlparse.parse(sql2)[0]
for token in parsed2.flatten():
    if token.normalized.upper() == 'ATTACH':
        attach_type = token.ttype

expected = T.Keyword

if detach_type == expected and attach_type == expected:
    print("NO CHANGE FOUND")
else:
    bug_vals = []
    fix_vals = []
    if detach_type != expected:
        bug_vals.append(f"DETACH={detach_type}")
        fix_vals.append(f"DETACH={expected}")
    if attach_type != expected:
        bug_vals.append(f"ATTACH={attach_type}")
        fix_vals.append(f"ATTACH={expected}")
    print(f"DIFFERENCE FOUND: {', '.join(bug_vals)} vs {', '.join(fix_vals)}")
