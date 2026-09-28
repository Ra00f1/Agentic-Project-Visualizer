"""Class-based tools. L2's default heuristic walks module-level defs;
these live under a class, so they exercise the "descend into class body"
path — @classmethod and @staticmethod both.
"""


class ClassTool:
    """Tool implemented as class methods rather than module-level fns."""

    @classmethod
    def load(cls, path: str) -> "ClassTool":
        """Classmethod tool. Constructs an instance."""
        return cls()

    @staticmethod
    def parse_static(text: str) -> dict:
        """Staticmethod tool. Independent of class state."""
        return {"parsed": True, "len": len(text)}
