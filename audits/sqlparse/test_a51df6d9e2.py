import timeit
import sqlparse


def test_reindent_tuple_list_not_quadratic():
    def make_sql(n):
        values = ", ".join(f"({i}, {i+1})" for i in range(n))
        return f"SELECT a, b FROM t WHERE (a, b) IN ({values})"

    def time_op(n):
        sql = make_sql(n)
        return min(timeit.repeat(lambda: sqlparse.format(sql, reindent=True), number=1, repeat=3))

    n = 100
    t_n = time_op(n)
    t_4n = time_op(n * 4)
    ratio = t_4n / t_n
    assert ratio < 9, f"growth ratio {ratio:.1f} suggests quadratic behaviour"
