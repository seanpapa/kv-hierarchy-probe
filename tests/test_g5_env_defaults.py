# SPDX-License-Identifier: Apache-2.0

"""The gds arm's cuFile pool size must stay 3072 MiB unless overridden.

The pool bounds how many concurrent requests the device path can serve, so
changing its default would silently move gate 5's envelope for every existing
result. These tests read the shell script itself rather than a copy of the
line, so they fail if the default is edited.
"""

# Standard
import re
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "g5_controlled_kv_experiment.sh"
DEFAULT_MIB = "3072"


def _expand(assignment: str, environment: dict[str, str]) -> str:
    """Evaluate one shell assignment and return the resulting value.

    Args:
        assignment: A shell assignment line taken verbatim from the script.
        environment: Variables exported before the assignment is evaluated.

    Returns:
        The value the shell assigns, with trailing newline stripped.
    """
    command = f"set -u; {assignment}; printf '%s' \"$PROBE_CUFILE_BUF_MIB\""
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", **environment},
    )
    return result.stdout


def _assignment_line() -> str:
    """Return the script's PROBE_CUFILE_BUF_MIB assignment.

    Returns:
        The assignment line, without surrounding whitespace.

    Raises:
        AssertionError: If the script does not define the variable exactly once.
    """
    lines = [
        line.strip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*PROBE_CUFILE_BUF_MIB=", line)
    ]
    if len(lines) != 1:
        raise AssertionError(f"expected one assignment, found {len(lines)}")
    return lines[0]


class CuFilePoolDefault(unittest.TestCase):
    def test_default_is_unchanged(self) -> None:
        self.assertEqual(_expand(_assignment_line(), {}), DEFAULT_MIB)

    def test_override_is_honored(self) -> None:
        for requested in ("1536", "2048", "6144"):
            with self.subTest(requested=requested):
                value = _expand(
                    _assignment_line(), {"PROBE_CUFILE_BUF_MIB": requested}
                )
                self.assertEqual(value, requested)

    def test_container_receives_the_variable_not_a_literal(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--env LMCACHE_CUFILE_BUFFER_SIZE="$PROBE_CUFILE_BUF_MIB"', body)
        self.assertNotIn("LMCACHE_CUFILE_BUFFER_SIZE=3072", body)

    def test_api_root_must_match_the_requested_model_path(self) -> None:
        """A served-name alias must not hide the wrong model load."""
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('[[ "$api_root" == "$MODEL" ]]', body)
        self.assertIn("model identity mismatch", body)


if __name__ == "__main__":
    unittest.main()


class PosixTierDirectory(unittest.TestCase):
    """One variable must drive both the tier the script creates and the one the
    client evicts. Two settings that must agree will eventually disagree."""

    DEFAULT = "/gds/lmcache-controlled-posix"

    def test_shell_default_is_unchanged_and_exported(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            f'export PROBE_POSIX_TIER_DIR="${{PROBE_POSIX_TIER_DIR:-{self.DEFAULT}}}"', body
        )

    def test_shell_uses_the_variable_not_the_literal(self) -> None:
        body = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(f"LMCACHE_LOCAL_DISK={self.DEFAULT}", body)
        self.assertIn('--env LMCACHE_LOCAL_DISK="$PROBE_POSIX_TIER_DIR"', body)

    def test_client_default_matches_the_shell(self) -> None:
        body = (SCRIPT.parent / "g5_controlled_kv_experiment.py").read_text(encoding="utf-8")
        self.assertIn(
            f'POSIX_TIER_DIR = os.environ.get("PROBE_POSIX_TIER_DIR", "{self.DEFAULT}")', body
        )

    def test_client_passes_the_path_as_an_argument(self) -> None:
        """Interpolating it into the program text would break on quotes."""
        body = (SCRIPT.parent / "g5_controlled_kv_experiment.py").read_text(encoding="utf-8")
        self.assertIn("Path(sys.argv[1]).rglob", body)
        self.assertIn('"-c", program, POSIX_TIER_DIR', body)
