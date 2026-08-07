#!/usr/bin/env python3
"""
Test runner for tm-monthly-backup test suite
"""

import sys
import unittest
from pathlib import Path

# Add the repo root to path so tests can import the `src` package
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _iter_tests(suite):
    """Flatten a (possibly nested) TestSuite into individual test cases."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for test in _iter_tests(item):
                yield test
        else:
            yield item


def build_suite(test_dir, pattern="test_*.py", exclude_modules=None):
    """
    Discover tests under test_dir matching pattern, optionally excluding
    tests whose module name is in exclude_modules.

    Args:
        test_dir: Directory to discover tests in.
        pattern: Test file glob pattern passed to unittest's discovery.
        exclude_modules: Optional iterable of module names (e.g.
            "test_integration") to drop from the discovered suite.

    Returns:
        A single unittest.TestSuite containing the selected tests.
    """
    loader = unittest.TestLoader()
    discovered = loader.discover(str(test_dir), pattern=pattern)

    if not exclude_modules:
        return discovered

    filtered = unittest.TestSuite()
    for test in _iter_tests(discovered):
        if test.__class__.__module__ not in exclude_modules:
            filtered.addTest(test)
    return filtered


def main():
    """Main test runner entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Run tm-monthly-backup test suite")

    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        '-v', '--verbose', '-vv', action='store_const', const=2,
        dest='verbosity', default=1,
        help='Verbose output: full test names and docstrings (-v and -vv are equivalent)')
    verbosity_group.add_argument(
        '-q', '--quiet', action='store_const', const=0, dest='verbosity',
        help='Minimal output: only the final summary')

    parser.add_argument('-p', '--pattern', default='test_*.py',
                       help='Test file pattern (default: test_*.py)')

    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument('--unit-only', action='store_true',
                       help='Run only unit tests (every test_*.py except test_integration.py)')
    selection_group.add_argument('--integration-only', action='store_true',
                       help='Run only integration tests (test_integration.py)')

    args = parser.parse_args()

    test_dir = Path(__file__).parent

    # Determine which tests to run based on options
    if args.unit_only:
        print("Running unit tests only...")
        suite = build_suite(test_dir, exclude_modules={'test_integration'})
    elif args.integration_only:
        print("Running integration tests only...")
        suite = build_suite(test_dir, pattern='test_integration.py')
    else:
        print("Running all tests...")
        suite = build_suite(test_dir, pattern=args.pattern)

    runner = unittest.TextTestRunner(verbosity=args.verbosity, buffer=True)
    result = runner.run(suite)

    # Print summary -- the single reporting path for every mode above
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print('='*60)
    print(f"Tests run: {result.testsRun}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Skipped: {len(result.skipped)}")

    if result.failures:
        print(f"\nFAILURES ({len(result.failures)}):")
        for test, traceback in result.failures:
            print(f"- {test}")

    if result.errors:
        print(f"\nERRORS ({len(result.errors)}):")
        for test, traceback in result.errors:
            print(f"- {test}")

    if result.wasSuccessful():
        print("\n✅ ALL TESTS PASSED!")
        sys.exit(0)
    else:
        print("\n❌ SOME TESTS FAILED!")
        sys.exit(1)

if __name__ == '__main__':
    main()
