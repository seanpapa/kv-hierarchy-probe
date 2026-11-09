# SPDX-License-Identifier: Apache-2.0
"""The version registry in docs/VERSIONING.md must match what the code writes."""

# Standard
import json
import re
import unittest
from pathlib import Path

# First Party
from kvprobe import __version__

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "VERSIONING.md"
SOURCES = sorted((ROOT / "kvprobe").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
VERSION_LITERAL = re.compile(r'"([0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.]+)?)"')
WRITTEN_FIELD = re.compile(r'"([a-z_]+_version)"\s*:')


def registry() -> dict[str, dict[str, set[str]]]:
    """Parse the registry table into written and read versions per field.

    Returns:
        Field name to ``{"written": ..., "read": ...}`` sets of version strings.
    """
    rows: dict[str, dict[str, set[str]]] = {}
    text = REGISTRY.read_text(encoding="utf-8")
    section = text.split("## Registry", 1)[1].split("\n## ", 1)[0]
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        field = cells[0].strip("`")
        rows[field] = {
            "written": set(re.findall(r"`([^`]+)`", cells[1])),
            "read": set(re.findall(r"`([^`]+)`", cells[2])),
        }
    return rows


def written_by_code() -> dict[str, set[str]]:
    """Collect every version field the code writes, with the literal values.

    Returns:
        Field name to the set of version strings written for it.
    """
    found: dict[str, set[str]] = {}
    for source in SOURCES:
        for line in source.read_text(encoding="utf-8").splitlines():
            match = WRITTEN_FIELD.search(line)
            if match is None:
                continue
            values = set(VERSION_LITERAL.findall(line[match.end():]))
            if values:
                found.setdefault(match.group(1), set()).update(values)
    for module, constant, field in (
        ("ingest.py", "ADAPTER_VERSION", "normalized_by.version"),
        ("report.py", "ASSESSMENT_VERSION", "assessment_version"),
    ):
        match = re.search(
            rf'^{constant} = "([^"]+)"',
            (ROOT / "kvprobe" / module).read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match is not None:
            found.setdefault(field, set()).add(match.group(1))
    package = re.search(
        r'^version = "([^"]+)"',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if package is not None:
        found["version"] = {package.group(1)}
    labels = {
        json.loads(path.read_text(encoding="utf-8"))["baseline_version"]
        for path in (ROOT / "examples" / "baselines").glob("*.json")
    }
    if labels:
        found["baseline_version"] = labels
    return found


class VersionRegistryTests(unittest.TestCase):
    """Keep the documented version registry and the code in agreement."""

    def test_package_and_import_version_match_public_baseline(self) -> None:
        """Installed metadata and the importable package name one baseline."""
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(written_by_code()["version"], {__version__})

    def test_baseline_format_is_distinct_from_evidence_schema(self) -> None:
        """Summary files identify their own format without an evidence key."""
        for path in (ROOT / "examples" / "baselines").glob("*.json"):
            with self.subTest(path=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(document["baseline_version"], "0.1.0")
                self.assertNotIn("schema_version", document)

    def test_every_version_field_the_code_writes_is_registered(self) -> None:
        """A new version field cannot ship without a registry row."""
        missing = sorted(set(written_by_code()) - set(registry()))
        self.assertEqual(missing, [], f"add these to docs/VERSIONING.md: {missing}")

    def test_registered_written_versions_match_the_code(self) -> None:
        """The written column is exactly what the code writes."""
        code = written_by_code()
        for field, row in registry().items():
            with self.subTest(field=field):
                self.assertEqual(row["written"], code.get(field, set()))

    def test_no_other_document_restates_the_current_assessment_version(self) -> None:
        """Enforce the "One registry" rule in the direction that actually drifted.

        The other tests compare the code with the registry table. Nothing
        checked prose, so when the verdict contract moved 0.4.0 → 0.5.0 five
        documents kept asserting 0.4 as current. Naming the *current* value
        outside the registry is what rots; naming a superseded one is history
        and stays allowed.

        Two documents are exempt because version numbers are their subject
        rather than a restatement: VERSIONING.md is the registry, and
        EVIDENCE.md explains the contract one version at a time ("`UNPROVEN`
        arrived with 0.4.0", "Results, 0.5.0"). Those sentences stay correct
        as the current version moves past them.
        """
        exempt = {REGISTRY, ROOT / "docs" / "EVIDENCE.md"}
        current = written_by_code()["assessment_version"]
        for path in sorted(ROOT.glob("**/*.md")):
            if path in exempt or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for version in current:
                with self.subTest(document=path.relative_to(ROOT), version=version):
                    self.assertNotIn(
                        version,
                        text,
                        f"{path.relative_to(ROOT)} states the current "
                        f"assessment version {version}; link to "
                        "docs/VERSIONING.md instead (its 'One registry' rule)",
                    )

    def test_registered_read_versions_appear_in_a_reader(self) -> None:
        """Every version listed as read is named on a line that handles the field."""
        sources = [path.read_text(encoding="utf-8") for path in SOURCES]
        for field, row in registry().items():
            key = field.split(".")[-1]
            for version in row["read"]:
                with self.subTest(field=field, version=version):
                    self.assertTrue(
                        any(
                            key in line and f'"{version}"' in line
                            for text in sources
                            for line in text.splitlines()
                        ),
                        f"{field} {version} is listed as read but no reader names it",
                    )


if __name__ == "__main__":
    unittest.main()
