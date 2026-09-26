import timeit
import sqlparse

def make_sql(n):
    return "\n".join(f"-- comment {i}" for i in range(n))

def time_op(n):
    sql = make_sql(n)
    return min(timeit.repeat(lambda: sqlparse.format(sql, strip_comments=True), number=1, repeat=3))

n = 500
t_n = time_op(n)
t_4n = time_op(n * 4)
ratio = t_4n / t_n
if ratio > 9:
    print("QUADRATIC")
else:
    print("LINEAR")
