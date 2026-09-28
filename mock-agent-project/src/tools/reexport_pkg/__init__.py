"""Re-exports `greet` so callers use `src.tools.reexport_pkg.greet`.

The Mongo `reexported_greeter` tool row points at this __init__'s
`greet`, not the underlying _impl module. L2 should follow the
`from ._impl import greet` line to the real definition.
"""

from ._impl import greet

__all__ = ["greet"]
