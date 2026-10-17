# Reusable constructs (Level 3)

This directory intentionally has **no `__init__.py`**. When `cdk` runs the app
from this folder, `infra/` is first on `sys.path`; a Python package named
`constructs` here would shadow the CDK `constructs` library and break synthesis.

Level 3 will add reusable constructs (network, data, service, monitoring) as
importable module files. If you introduce package imports for them, move the
modules into a differently named package (e.g. `infra/libs/`) instead of adding
an `__init__.py` here.
