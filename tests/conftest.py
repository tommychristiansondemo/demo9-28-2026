import importlib.util
import os
import sys


def load_lambda_module(module_name: str, path: str):
    """Load a Lambda handler module under a distinct name so two handlers
    both named handler.py don't collide via a bare sys.path + import handler
    (confirmed footgun from a prior class demo in this series: a MagicMock/
    module-cache collision when two Lambdas share the literal filename).
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
