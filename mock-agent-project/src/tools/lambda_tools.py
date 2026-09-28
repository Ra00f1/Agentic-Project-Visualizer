"""Lambda-defined tool. Edge case for AST-based function extraction:
`add_one` is not a `def`, it's an assignment. Some extractors will miss
it. That's the specific behavior this fixture tests.
"""

# Assignments of lambdas to module-level names — the AST node is Assign,
# not FunctionDef. L2 should either resolve this (walking Assign.value)
# or explicitly flag it as unresolved.
add_one = lambda x: x + 1
add_two = lambda x: x + 2
