import timeit
import sqlparse

def make_sql(n):
    # Use nested tuple list that triggers the reindent grouping path
    # Based on GHSA-cfqr-cjx5-5jcm: tuple list in WHERE clause
    values = ", ".join(f"({i}, {i+1})" for i in range(n))
    return f"SELECT a, b FROM t WHERE (a, b) IN ({values})"

def time_op(n):
    sql = make_sql(n)
    return min(timeit.repeat(lambda: sqlparse.format(sql, reindent=True), number=1, repeat=3))

n = 100
t_n = time_op(n)
t_4n = time_op(n * 4)
ratio = t_4n / t_n
if ratio > 9:
    print("QUADRATIC")
else:
    print("LINEAR")
