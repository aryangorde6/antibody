import timeit
import sqlparse

def test_strip_comments_not_quadratic():
    def make_sql(n):
        return "\n".join(f"-- comment {i}" for i in range(n))

    def time_op(n):
        sql = make_sql(n)
        return min(timeit.repeat(lambda: sqlparse.format(sql, strip_comments=True), number=1, repeat=3))

    n = 500
    t_n = time_op(n)
    t_4n = time_op(n * 4)
    ratio = t_4n / t_n
    assert ratio < 9, f"growth ratio {ratio:.1f} suggests quadratic behaviour"
