"""Mock agentic project — root package.

This file exists so `python -m compileall src` walks the tree cleanly and so
the scanner can treat `src/` as an importable package during resolution.
"""

__version__ = "0.0.0-mock"
