"""Dynamic dispatch — the target function is looked up at runtime.
Static analysis loses these edges; the visualizer should flag them
rather than pretend they don't exist.
"""

REGISTRY = {}


def register(name):
    def _decorator(fn):
        REGISTRY[name] = fn
        return fn
    return _decorator


@register("alpha")
def handle_alpha(x):
    return x * 2


@register("beta")
def handle_beta(x):
    return x + 100


def dispatch(name, x):
    """Runtime dispatch — L3 cannot statically resolve which handler runs."""
    return REGISTRY[name](x)


def getattr_dispatch(module, fn_name, arg):
    """The getattr flavor — same limitation."""
    fn = getattr(module, fn_name)
    return fn(arg)
