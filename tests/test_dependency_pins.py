"""
Tests for dependency floors, ceilings, and the lockfile (issue #59).

``pyproject.toml``'s dependency floors previously admitted specific
known-vulnerable releases on code paths this tool exercises directly (Pillow
10.0.0, satisfied by the old ``pillow>=10.0.0`` floor, carries CVE-2023-4863
-- a bundled-libwebp heap buffer overflow reachable through any ``.webp`` in
``export/``, since ``.webp`` is in ``PHOTO_EXTENSIONS`` -- plus
CVE-2023-44271 and, until 10.3.0, CVE-2024-28219). These tests parse
``pyproject.toml`` directly (via regex rather than a TOML library, since this
project's floor is Python 3.9 and ``tomllib`` needs 3.11+) rather than
trusting the file was edited correctly, so a future accidental downgrade of
a floor is caught mechanically instead of only by review.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"
REQUIREMENTS_LOCK = REPO_ROOT / "requirements.lock"

# The minimum floor each dependency must meet or exceed, and why (issue #59).
# Pillow's is CVE-driven: 10.3.0 is the earliest release clearing all three
# CVEs named in the issue (CVE-2023-4863 and CVE-2023-44271, both fixed in
# 10.0.1; CVE-2024-28219, fixed only in 10.3.0). pillow-heif's 0.16.0 is the
# first release requiring libheif>=1.19.0, well past the libheif versions
# carrying CVE-2023-0996 and CVE-2024-41311. The other three floors are not
# individually CVE-driven -- they simply match or exceed the versions this
# project's own test suite has verified safe -- and are pinned here as a
# floor regression guard regardless.
MINIMUM_FLOORS = {
    "click": (8, 1, 7),
    "pillow": (10, 3, 0),
    "pillow-heif": (0, 16, 0),
    "python-dateutil": (2, 8, 2),
    "rich": (13, 7, 0),
    "hachoir": (3, 3, 0),
}


def _parse_version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def _extract_pins(text: str) -> dict:
    """Map each dependency name to its (floor, ceiling) version strings."""
    pins = {}
    for match in re.finditer(
        r'"([a-zA-Z0-9_-]+)>=([\d.]+),<(\d+)"', text
    ):
        name, floor, ceiling = match.groups()
        pins[name] = (floor, ceiling)
    return pins


class TestPyprojectFloorsExcludeKnownVulnerableReleases(unittest.TestCase):
    """Every dependency floor in pyproject.toml meets or exceeds issue #59's
    security-driven minimum, parsed directly from the file rather than
    assumed from what the source was last edited to say."""

    def setUp(self):
        self.text = PYPROJECT.read_text()
        self.pins = _extract_pins(self.text)

    def test_every_expected_dependency_is_declared(self):
        self.assertEqual(set(self.pins), set(MINIMUM_FLOORS))

    def test_every_floor_meets_or_exceeds_the_minimum(self):
        for name, minimum in MINIMUM_FLOORS.items():
            floor_str, _ceiling_str = self.pins[name]
            floor = _parse_version(floor_str)
            self.assertGreaterEqual(
                floor, minimum,
                "%s floor %s is below the issue #59 minimum %s"
                % (name, floor_str, ".".join(map(str, minimum))),
            )

    def test_every_dependency_has_an_upper_bound(self):
        for name, (_floor, ceiling) in self.pins.items():
            self.assertTrue(
                ceiling.isdigit() and int(ceiling) > 0,
                "%s has no sane upper bound" % name,
            )


class TestRequirementsTxtMirrorsPyproject(unittest.TestCase):
    """requirements.txt's pins must exactly mirror pyproject.toml's, since
    the docstring at the top of both files says pyproject.toml is the single
    source of truth and requirements.txt is only a convenience pointer."""

    def test_pins_are_byte_identical_to_pyproject(self):
        pyproject_pins = _extract_pins(PYPROJECT.read_text())

        requirements_text = REQUIREMENTS_TXT.read_text()
        requirements_pins = {}
        for line in requirements_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"([a-zA-Z0-9_-]+)>=([\d.]+),<(\d+)", line)
            self.assertIsNotNone(match, "unparseable requirements.txt line: %r" % line)
            name, floor, ceiling = match.groups()
            requirements_pins[name] = (floor, ceiling)

        self.assertEqual(requirements_pins, pyproject_pins)


class TestLockfileExistsAndCoversEveryDirectDependency(unittest.TestCase):
    """A hash-pinned lockfile is committed (issue #59's acceptance
    criterion) and pins every direct dependency within the range
    pyproject.toml declares."""

    def setUp(self):
        self.assertTrue(
            REQUIREMENTS_LOCK.exists(),
            "requirements.lock is missing -- issue #59 requires a committed, "
            "hash-pinned lockfile",
        )
        self.lock_text = REQUIREMENTS_LOCK.read_text()
        self.pyproject_pins = _extract_pins(PYPROJECT.read_text())

    def test_lockfile_documents_the_require_hashes_install_command(self):
        self.assertIn(
            "--require-hashes",
            self.lock_text,
            "requirements.lock does not document the --require-hashes "
            "install command anywhere",
        )

    def test_every_direct_dependency_is_pinned_with_a_hash_within_range(self):
        for name, (floor_str, ceiling_str) in self.pyproject_pins.items():
            # requirements.lock pins by the PyPI distribution name, which is
            # lowercase with hyphens for these six -- matches pyproject.toml's
            # own spelling for all of them.
            pattern = re.compile(
                r"^%s==([\d.]+(?:\.post\d+)?) \\\n(?:\s+--hash=sha256:[0-9a-f]{64} ?\\?\n?)+"
                % re.escape(name),
                re.MULTILINE,
            )
            match = pattern.search(self.lock_text)
            self.assertIsNotNone(
                match, "requirements.lock has no hash-pinned entry for %s" % name
            )
            # Compare only the (major, minor, patch) prefix: a lockfile pin
            # can carry a post-release suffix (e.g. "2.9.0.post0") that the
            # simple int-tuple comparison below does not need to parse.
            pinned = tuple(int(p) for p in match.group(1).split(".")[:3])
            floor = _parse_version(floor_str)
            ceiling_major = int(ceiling_str)
            self.assertGreaterEqual(
                pinned, floor,
                "%s is pinned in requirements.lock below pyproject.toml's own floor"
                % name,
            )
            self.assertLess(
                pinned[0], ceiling_major,
                "%s is pinned in requirements.lock at or above pyproject.toml's "
                "own ceiling" % name,
            )


if __name__ == "__main__":
    unittest.main()
