# SPDX-License-Identifier: Apache-2.0
"""The launchers stay local by default and only touch what they own (S1/S2).

The serving API has no authentication, so it binds loopback unless the
operator opens it. A launcher removes only containers it labelled, empties
the tier only under a real mount, writes evidence only into a directory the
operator owns, and runs its client from the repository rather than /tmp.
"""

# Standard
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LAUNCHERS = (
    SCRIPTS / "g5_controlled_kv_experiment.sh",
    SCRIPTS / "g2_context_sweep.sh",
    SCRIPTS / "g3_admission_cost.sh",
    SCRIPTS / "g4_capacity_sweep.sh",
    SCRIPTS / "tools" / "nixl_matched.sh",
    SCRIPTS / "tools" / "nixl_mode_matrix.sh",
)
SERVING_LAUNCHERS = tuple(path for path in LAUNCHERS if "vllm serve" in path.read_text())


def _function(script: Path, name: str) -> str:
    """Return one bash function's source from a launcher."""
    match = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}", script.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{name} not found in {script.name}")
    return match.group(0)


def _bash(source: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet and capture its result."""
    return subprocess.run(
        ["bash", "-c", source], capture_output=True, text=True, env=env, check=False
    )


class BindAndPathRules(unittest.TestCase):
    """Text rules every launcher follows."""

    def test_serving_api_binds_loopback_unless_opened(self) -> None:
        """No launcher hard-codes an all-interfaces bind for vLLM."""
        for launcher in SERVING_LAUNCHERS:
            with self.subTest(launcher=launcher.name):
                text = launcher.read_text(encoding="utf-8")
                self.assertNotIn("--host 0.0.0.0", text)
                self.assertIn("SERVE_HOST=${SERVE_HOST:-127.0.0.1}", text)

    def test_every_started_container_is_labelled(self) -> None:
        """Ownership is what makes a later removal safe."""
        for launcher in SERVING_LAUNCHERS:
            text = launcher.read_text(encoding="utf-8")
            with self.subTest(launcher=launcher.name):
                runs = re.findall(r"docker run -d --name [^\n]*", text)
                self.assertTrue(runs)
                for run in runs:
                    self.assertIn("kvprobe.owner=$PROBE_OWNER", run)

    def test_containers_are_removed_only_through_the_ownership_check(self) -> None:
        """A bare `docker rm -f` of a named container would bypass the label."""
        for launcher in LAUNCHERS:
            text = launcher.read_text(encoding="utf-8")
            body = text.replace(_function(launcher, "remove_owned_container"), "") if (
                "remove_owned_container()" in text
            ) else text
            with self.subTest(launcher=launcher.name):
                self.assertIsNone(re.search(r'docker rm -f "\$', body))

    def test_clients_are_not_run_from_tmp_and_use_loopback(self) -> None:
        """A fixed /tmp path can be written first by another user."""
        for launcher in LAUNCHERS:
            text = launcher.read_text(encoding="utf-8")
            with self.subTest(launcher=launcher.name):
                self.assertIsNone(re.search(r"CLIENT=\$\{CLIENT:-/tmp/", text))
                self.assertNotIn("python3 /tmp/", text)
                self.assertNotIn("http://localhost:", text)


class HostCacheRule(unittest.TestCase):
    """Dropping the host page cache is an explicit opt-in."""

    def test_drop_caches_only_behind_the_opt_in(self) -> None:
        """Every client that can drop the host cache gates it on PROBE_DROP_HOST_CACHE."""
        for client in sorted(SCRIPTS.glob("*.py")):
            text = client.read_text(encoding="utf-8")
            command = "tee /proc/sys/vm/drop_caches"
            if command not in text:
                continue
            with self.subTest(client=client.name):
                self.assertIn('os.environ.get("PROBE_DROP_HOST_CACHE") == "1"', text)
                self.assertEqual(text.count(command), 1)

    def test_g3_does_not_drop_the_cache_by_default(self) -> None:
        """Without the opt-in the admission client never shells out to drop_caches."""
        env = {key: value for key, value in os.environ.items() if key != "PROBE_DROP_HOST_CACHE"}
        result = subprocess.run(
            [
                "python3",
                "-c",
                "import sys, unittest.mock as m; sys.path.insert(0, 'scripts');"
                "import g3_admission_cost as g;"
                "r = m.patch('subprocess.run').start(); g.drop_page_cache();"
                "print(r.call_count)",
            ],
            cwd=SCRIPTS.parent,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.stdout.strip(), "0", result.stderr)


class SafetyFunctions(unittest.TestCase):
    """The guard functions behave as documented when run."""

    def test_posix_tier_dir_must_be_one_level_under_the_mount(self) -> None:
        """Emptying /gds itself, or a path that climbs out, is refused."""
        function = _function(SCRIPTS / "g5_controlled_kv_experiment.sh", "check_posix_tier_dir")
        for path, accepted in (
            ("/gds/lmcache-controlled-posix", True),
            ("/gds", False),
            ("/gds/", False),
            ("/gds/..", False),
            ("/gds/a/b", False),
            ("/etc", False),
        ):
            with self.subTest(path=path):
                result = _bash(f"{function}\ncheck_posix_tier_dir '{path}'")
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_evidence_dir_refuses_a_symlink(self) -> None:
        """A pre-placed symlink would redirect the evidence somewhere else."""
        function = _function(SCRIPTS / "g5_controlled_kv_experiment.sh", "prepare_evidence_dir")
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(directory) / "evidence"
            self.assertEqual(_bash(f"{function}\nprepare_evidence_dir '{fresh}'").returncode, 0)
            self.assertTrue(fresh.is_dir())
            link = Path(directory) / "link"
            link.symlink_to(fresh)
            self.assertNotEqual(_bash(f"{function}\nprepare_evidence_dir '{link}'").returncode, 0)

    def _remove(self, label: str) -> tuple[int, str]:
        """Run remove_owned_container against a stub docker; return (status, calls)."""
        function = _function(SCRIPTS / "g5_controlled_kv_experiment.sh", "remove_owned_container")
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            stub = Path(directory) / "docker"
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "$*" >> "{calls}"\n'
                'if [[ "$1" == inspect ]]; then\n'
                f'  [[ "{label}" == absent ]] && exit 1\n'
                f'  echo "{label}"\n'
                "fi\n"
            )
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}")
            result = _bash(
                f"PROBE_OWNER=kv-hierarchy-probe\n{function}\nremove_owned_container kv-controlled",
                env,
            )
            return result.returncode, calls.read_text() if calls.exists() else ""

    def test_a_labelled_container_is_removed(self) -> None:
        status, calls = self._remove("kv-hierarchy-probe")
        self.assertEqual(status, 0)
        self.assertIn("rm -f kv-controlled", calls)

    def test_an_unlabelled_container_is_left_alone(self) -> None:
        status, calls = self._remove("<no value>")
        self.assertNotEqual(status, 0)
        self.assertNotIn("rm -f", calls)

    def test_an_absent_container_is_nothing_to_do(self) -> None:
        status, calls = self._remove("absent")
        self.assertEqual(status, 0)
        self.assertNotIn("rm -f", calls)


if __name__ == "__main__":
    unittest.main()
