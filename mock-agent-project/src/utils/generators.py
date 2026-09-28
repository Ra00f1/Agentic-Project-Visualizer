"""Generator functions. AST walker should treat these the same as
regular functions (call graph works; the yield is body-level detail)."""

def counting():
    i = 0
    while True:
        yield i
        i += 1


def batched(source, size):
    batch = []
    for item in source:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
