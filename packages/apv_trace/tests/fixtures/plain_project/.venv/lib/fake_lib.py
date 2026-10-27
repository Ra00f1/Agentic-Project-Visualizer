"""Lives under `.venv/lib`, nested INSIDE the fixture's own root -- proves
the virtualenv-directory-name filter excludes it regardless of nesting
depth, not just when it sits outside the traced root entirely."""


def vendored_helper() -> str:
    return "vendored"
