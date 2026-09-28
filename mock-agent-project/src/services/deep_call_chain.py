"""Deep call chain — 8 hops. Stresses L3's call graph extraction depth."""

def hop1(x): return hop2(x)
def hop2(x): return hop3(x)
def hop3(x): return hop4(x)
def hop4(x): return hop5(x)
def hop5(x): return hop6(x)
def hop6(x): return hop7(x)
def hop7(x): return hop8(x)
def hop8(x): return x + 1
