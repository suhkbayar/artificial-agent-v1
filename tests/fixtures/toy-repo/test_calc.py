import sys

from calc import add, divide

assert add(2, 3) == 5
assert divide(6, 3) == 2

try:
    divide(1, 0)
except ValueError:
    pass
else:
    print("FAIL: divide(1, 0) must raise ValueError")
    sys.exit(1)

print("ok")
