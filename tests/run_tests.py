#!/usr/bin/env python3
"""
Test runner for tm-monthly-backup test suite
"""

import sys
import unittest
import os
from pathlib import Path

# Add src to path so tests can import modules
project_root = Path(__file__).parent.parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

def run_tests(verbosity=2, pattern="test_*.py"):
    """
    Run all tests in the tests directory.

    Args:
        verbosity: Test output verbosity level (0-2)
        pattern: Test file pattern to match

    Returns:
        TestResult object
    """
    # Discover and run all tests
    loader = unittest.TestLoader()
    test_dir = Path(__file__).parent

    # Load all test modules
    suite = loader.discover(str(test_dir), pattern=pattern)

    # Run tests with specified verbosity
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=True)
    result = runner.run(suite)

    return result

def main():
    """Main test runner entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Run tm-monthly-backup test suite")
    parser.add_argument('-v', '--verbose', action='count', default=2,
                       help='Increase test output verbosity')
    parser.add_argument('-q', '--quiet', action='store_const', const=0, dest='verbose',
                       help='Minimal test output')
    parser.add_argument('-p', '--pattern', default='test_*.py',
                       help='Test file pattern (default: test_*.py)')
    parser.add_argument('--unit-only', action='store_true',
                       help='Run only unit tests (exclude integration tests)')
    parser.add_argument('--integration-only', action='store_true',
                       help='Run only integration tests')

    args = parser.parse_args()

    # Determine test pattern based on options
    if args.unit_only:
        pattern = 'test_exif_handler.py test_file_categorizer.py'
        print("Running unit tests only...")
    elif args.integration_only:
        pattern = 'test_integration.py'
        print("Running integration tests only...")
    else:
        pattern = args.pattern
        print("Running all tests...")

    # Special handling for multiple specific files
    if args.unit_only:
        # Run unit tests individually
        success = True
        for test_file in ['test_exif_handler.py', 'test_file_categorizer.py']:
            print(f"\n{'='*60}")
            print(f"Running {test_file}")
            print('='*60)
            result = run_tests(verbosity=args.verbose, pattern=test_file)
            if not result.wasSuccessful():
                success = False

        sys.exit(0 if success else 1)
    else:
        # Run with standard discovery
        result = run_tests(verbosity=args.verbose, pattern=pattern)

    # Print summary
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