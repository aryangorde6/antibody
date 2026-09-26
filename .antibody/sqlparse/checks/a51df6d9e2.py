import timeit
import sqlparse

def make_tuple_sql(n):
    values = ", ".join(f"({i}, {i+1})" for i in range(n))
    return f"SELECT * FROM t WHERE (a, b) IN ({values})"

n = 200

def time_format(size):
    sql = make_tuple_sql(size)
    return min(timeit.repeat(lambda: sqlparse.format(sql, reindent=True), number=1, repeat=5))

t_n = time_format(n)
t_4n = time_format(4 * n)

ratio = t_4n / t_n

if ratio >= 8:
    print(f"DIFFERENCE FOUND: ratio={ratio:.1f} (quadratic) vs ratio<8 (linear)")
else:
    print(f"NO CHANGE FOUND (ratio={ratio:.1f})")
