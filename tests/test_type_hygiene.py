"""
Tests for type-hint and import hygiene in ``src/`` (issue #20).

Three defects, verified statically against the actual source (not behavior,
since none of these change what the tool does):

1. ``Dict[str, any]`` on ``FileProcessor.process_all_files``,
   ``FileProcessor._generate_summary``, and the ``_quarantined_files``
   attribute referenced the builtin aggregation function ``any``, not
   ``typing.Any`` -- a type checker rejects it outright, and it documented
   the wrong contract to a human reader too.
2. Unconditional, function-local ``import`` statements (a stale
   ``from PIL import Image, UnidentifiedImageError`` in
   ``FileCategorizer._read_image_metadata`` and two ``import re`` in
   ``ExifHandler``) obscured each module's actual dependencies for no
   benefit -- they are not conditional on any runtime feature check.
   The one deliberately lazy import this project keeps,
   ``ExifHandler._ensure_hachoir_imported`` (issue #47, avoids paying for
   hachoir on a photo-only run), is explicitly exempted here and must stay
   function-local.
3. Bare ``except:`` clauses were already eliminated by #16/#39; this test
   locks that invariant in going forward rather than merely re-confirming
   it by eye.
"""

import ast
import inspect
import unittest
from pathlib import Path
from typing import Any, Dict

from src.file_processor import FileProcessor

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# The single lazy import this project deliberately keeps function-local
# (issue #47). Any other function-local import is a hygiene regression.
ALLOWED_LOCAL_IMPORT_FUNCTIONS = {"_ensure_hachoir_imported"}


class TestAnyTypeHints(unittest.TestCase):
    """``Dict[str, Any]`` must use ``typing.Any``, not the builtin ``any``."""

    def test_process_all_files_return_annotation_uses_typing_any(self):
        sig = inspect.signature(FileProcessor.process_all_files)
        self.assertEqual(sig.return_annotation, Dict[str, Any])

    def test_generate_summary_return_annotation_uses_typing_any(self):
        sig = inspect.signature(FileProcessor._generate_summary)
        self.assertEqual(sig.return_annotation, Dict[str, Any])

    def test_no_builtin_any_used_as_a_type_subscript_in_source(self):
        """Guards the ``self._quarantined_files: List[Dict[str, any]]``
        instance-attribute annotation, which ``inspect.signature`` cannot
        see (it is a local annotated assignment, not stored on the class or
        function), by scanning the source text directly."""
        source = (SRC_DIR / "file_processor.py").read_text()
        self.assertNotIn("Dict[str, any]", source)


class TestNoBareExcept(unittest.TestCase):
    """Bare ``except:`` clauses were removed by #16/#39; keep them out."""

    def test_no_bare_except_anywhere_in_src(self):
        offenders = []
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [])


class TestNoStrayFunctionLocalImports(unittest.TestCase):
    """Unconditional imports belong at module level, so a reader can see a
    module's real dependencies without reading every function body. The one
    exception is the hachoir lazy-loader (issue #47), kept local on purpose."""

    def test_no_unconditional_local_imports_outside_the_lazy_loader(self):
        offenders = []
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if func.name in ALLOWED_LOCAL_IMPORT_FUNCTIONS:
                    continue
                for node in ast.walk(func):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        offenders.append(
                            "%s:%d %s" % (path.name, node.lineno, func.name)
                        )
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
