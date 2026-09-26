import timeit
import sqlparse

# Use larger n so quadratic behavior is unmistakable
# With bug: n=1000 takes ~100ms, n=4000 takes ~1600ms (ratio ~16)
# With fix: n=1000 takes ~few ms, n=4000 takes ~few ms (ratio ~4)

def time_op(n):
    s = "-- c\n" * n
    return min(timeit.repeat(lambda: sqlparse.format(s, strip_comments=True), number=1, repeat=5))

n = 1000
t_n = time_op(n)
t_4n = time_op(4 * n)
ratio = t_4n / t_n

if ratio >= 8:
    print(f"DIFFERENCE FOUND: growth ratio {ratio:.1f} (quadratic, bug present) vs ratio < 8 (linear, bug fixed)")
else:
    print(f"NO CHANGE FOUND: growth ratio {ratio:.1f} (linear behaviour, fix present)")
