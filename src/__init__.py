# tm-monthly-backup package

# The single source of truth for this project's version.
#
# ``pyproject.toml`` reads it via ``[tool.setuptools.dynamic]``
# (``version = {attr = "src.__version__"}``) and ``src/main.py`` passes it to
# ``click.version_option``, so ``pip`` metadata and ``--version`` can no longer
# disagree. They previously did: both carried a hardcoded ``"1.0.0"`` and
# neither was updated across the 1.1.0, 1.2.0 and 1.3.0 milestones, so
# ``--version`` reported 1.0.0 for a build containing three milestones of work.
#
# This module is imported for its side effect of being a package root by every
# ``src.*`` import, so it must stay dependency-free -- do not add imports here.
__version__ = "1.3.0"
