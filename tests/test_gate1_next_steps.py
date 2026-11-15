# SPDX-License-Identifier: Apache-2.0
"""Gate 1 next steps: what a non-PASS result is missing, and what to do next."""

# Standard
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
import io
import json
import random
import re
import tempfile
import unittest

# First Party
from kvprobe.cli import main
from kvprobe.report import (
    Gate1NextStep,
    assess_evidence,
    gate1_next_steps,
    render_markdown,
    render_text,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "evidence"
GATE1_DOC = ROOT / "docs" / "GATE1.md"
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)

NOT_FILTERED = "Counter not filtered to the tier's device"
NOT_PER_REQUEST = "Not observed per request"
HEURISTIC = "Heuristic attribution"
NO_EXPECTED = "No expected bytes"
NO_TARGET = "No target-tier request"
WRONG = "Wrong output"
NOTHING = "Tier device read nothing"
SHORT = "Fewer bytes than expected"
EXCESS = "More bytes than expected on the tier's device"
ALL_TITLES = {
    NOT_FILTERED,
    NOT_PER_REQUEST,
    HEURISTIC,
    NO_EXPECTED,
    NO_TARGET,
    WRONG,
    NOTHING,
    SHORT,
    EXCESS,
}


def _passing() -> dict:
    """Return the bundled single-request PASS evidence."""
    return json.loads((EXAMPLES / "pass-all-attempts.json").read_text(encoding="utf-8"))


def _with_request(**changes: object) -> dict:
    """Return PASS evidence whose one request is changed.

    Args:
        **changes: Request fields, or ``granularity``, ``confidence`` and
            ``target_device_filtered`` for its attribution.

    Returns:
        A new evidence document.
    """
    payload = _passing()
    request = payload["evidence"]["attribution"]["requests"][0]
    for name, value in changes.items():
        if name in {"granularity", "confidence", "target_device_filtered"}:
            request["attribution"][name] = value
        else:
            request[name] = value
    return payload


def _steps(payload: dict) -> tuple[str, tuple[Gate1NextStep, ...]]:
    """Assess evidence and return the Gate 1 status with its next steps."""
    assessment = assess_evidence(payload)
    return assessment["gates"][0]["status"], gate1_next_steps(payload, assessment)


def _anchors(path: Path) -> set[str]:
    """Return GitHub-style heading anchors and explicit anchors of a document."""
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    anchors = set(re.findall(r'<a\s+(?:name|id)="([^"]+)"', text))
    seen: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.*?)\s*$", text, re.M):
        slug = re.sub(r"<[^>]+>", "", heading).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", slug).replace(" ", "-")
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return anchors


CASES = {
    "unfiltered counter": ({"target_device_filtered": False}, "UNPROVEN", [NOT_FILTERED]),
    "per-batch observation": ({"granularity": "per_batch"}, "UNPROVEN", [NOT_PER_REQUEST]),
    "heuristic attribution": ({"confidence": "heuristic"}, "UNPROVEN", [HEURISTIC]),
    "no expected bytes": ({"expected_bytes": 0, "observed_bytes": 0}, "UNPROVEN", [NO_EXPECTED]),
    "bytes read with none expected": ({"expected_bytes": 0}, "FAIL", [EXCESS, NO_EXPECTED]),
    "no target-tier request": ({"source": "upper_tier"}, "FAIL", [NO_TARGET]),
    "wrong output": ({"correct": False}, "FAIL", [WRONG]),
    "nothing read": ({"observed_bytes": 0}, "FAIL", [NOTHING]),
    "fewer bytes": ({"observed_bytes": 134217728}, "FAIL", [SHORT]),
    "more bytes on a filtered counter": ({"observed_bytes": 536870912}, "FAIL", [EXCESS]),
    "nothing read on an unfiltered counter": (
        {"observed_bytes": 0, "target_device_filtered": False},
        "FAIL",
        [NOTHING, NOT_FILTERED],
    ),
    "per-batch heuristic counts once": (
        {"granularity": "per_batch", "confidence": "heuristic"},
        "UNPROVEN",
        [NOT_PER_REQUEST],
    ),
    "wrong output and a heuristic": (
        {"correct": False, "confidence": "heuristic"},
        "FAIL",
        [WRONG, HEURISTIC],
    ),
}


class Gate1NextStepTests(unittest.TestCase):
    """Each Gate 1 finding maps to one step; verdicts never change."""

    def test_each_finding_gives_its_steps_in_order(self) -> None:
        """Violations come first, then missing evidence, and nothing is hidden."""
        for name, (changes, status, titles) in CASES.items():
            with self.subTest(case=name):
                found, steps = _steps(_with_request(**changes))
                self.assertEqual(found, status)
                self.assertEqual([step.title for step in steps], titles)

    def test_every_title_is_reachable(self) -> None:
        """The cases above exercise every step the code can give."""
        reached = {title for _, _, titles in CASES.values() for title in titles}
        self.assertEqual(reached, ALL_TITLES)

    def test_device_findings_count_target_tier_requests_only(self) -> None:
        """A recompute request's counter does not count as missing tier evidence."""
        payload = _with_request(target_device_filtered=False)
        requests = payload["evidence"]["attribution"]["requests"]
        recompute = deepcopy(requests[0])
        recompute.update(id="request-2", source="recompute")
        requests.append(recompute)
        payload["bindings"]["attribution_requests"].append(
            {"request_id": "request-2", "configuration_id": "config-a"}
        )
        status, steps = _steps(payload)
        self.assertEqual(status, "UNPROVEN")
        self.assertEqual([step.title for step in steps], [NOT_FILTERED])
        self.assertIn("for 1/1 target-tier requests", steps[0].finding)

    def test_tolerance_is_named_in_byte_findings(self) -> None:
        """A byte finding states the tolerance it was judged against."""
        payload = _with_request(observed_bytes=134217728)
        payload["evidence"]["attribution"]["tolerance_fraction"] = 0.05
        _, steps = _steps(payload)
        self.assertIn("beyond the 5% tolerance", steps[0].finding)

    def test_a_pass_has_no_steps(self) -> None:
        """A passing Gate 1 adds nothing to the report."""
        status, steps = _steps(_passing())
        self.assertEqual((status, steps), ("PASS", ()))

    def test_bundled_examples(self) -> None:
        """Only a Gate 1 FAIL or UNPROVEN gets steps, and the reason is unchanged."""
        expected = {
            "attribution-unfiltered-counter.json": [NOT_FILTERED],
            "phantom-tier-framework-hit.json": [NOTHING, NOT_FILTERED],
        }
        for path in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(example=path.name):
                _, steps = _steps(json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(
                    [step.title for step in steps], expected.get(path.name, [])
                )

    def test_every_non_pass_result_is_explained(self) -> None:
        """Across mixed requests, FAIL names a violation, UNPROVEN only missing evidence."""
        rng = random.Random(15)
        base = _passing()
        template = base["evidence"]["attribution"]["requests"][0]
        expected = template["expected_bytes"]
        seen = set()
        for _ in range(1500):
            payload = deepcopy(base)
            requests = []
            for index in range(rng.randint(1, 4)):
                request = deepcopy(template)
                request["id"] = f"request-{index + 1}"
                request["source"] = rng.choice(["target_tier", "upper_tier", "recompute"])
                request["correct"] = rng.random() > 0.15
                request["expected_bytes"] = rng.choice([0, expected, expected])
                request["observed_bytes"] = rng.choice(
                    [0, expected // 2, expected, expected * 2]
                )
                attribution = request["attribution"]
                attribution["granularity"] = rng.choice(
                    ["per_request", "per_request", "per_batch", "per_run"]
                )
                attribution["confidence"] = rng.choice(["direct", "corroborating", "heuristic"])
                attribution["target_device_filtered"] = rng.random() > 0.3
                requests.append(request)
            payload["evidence"]["attribution"]["requests"] = requests
            payload["bindings"]["attribution_requests"] = [
                {"request_id": request["id"], "configuration_id": "config-a"}
                for request in requests
            ]
            status, steps = _steps(payload)
            seen.add(status)
            kinds = {step.kind for step in steps}
            if status == "PASS":
                self.assertEqual(steps, ())
            elif status == "UNPROVEN":
                self.assertTrue(steps)
                self.assertEqual(kinds, {"Missing"})
            else:
                titles = {step.title for step in steps}
                self.assertTrue("Observed" in kinds or NO_TARGET in titles, titles)
        self.assertEqual(seen, {"PASS", "UNPROVEN", "FAIL"})

    def test_steps_do_not_prescribe_a_pass(self) -> None:
        """No step tells the reader to weaken the measurement to get a PASS."""
        banned = re.compile(r"\b(disable|turn off|loosen|to pass|PASS)\b", re.I)
        for name, (changes, _, _) in CASES.items():
            _, steps = _steps(_with_request(**changes))
            for step in steps:
                with self.subTest(case=name, step=step.title):
                    text = " ".join((step.finding, step.why, step.action, step.limit))
                    self.assertIsNone(banned.search(text), text)

    def test_references_resolve(self) -> None:
        """Every document and anchor a step points to exists."""
        for name, (changes, _, _) in CASES.items():
            _, steps = _steps(_with_request(**changes))
            for step in steps:
                for target in step.see:
                    with self.subTest(case=name, target=target):
                        path, _, anchor = target.partition("#")
                        self.assertTrue((ROOT / path).is_file(), path)
                        if anchor:
                            self.assertIn(anchor, _anchors(ROOT / path))

    def test_reference_table_lists_every_title(self) -> None:
        """docs/GATE1.md names exactly the titles the report can print."""
        text = FENCE.sub("", GATE1_DOC.read_text(encoding="utf-8"))
        section = text.split("### Next steps in the report", 1)[1].split("\n## ", 1)[0]
        row = re.compile(r"^\| (?:`FAIL`|`UNPROVEN`|`FAIL` or `UNPROVEN`) \| ([^|]+?) \|", re.M)
        rows = set(row.findall(section))
        self.assertEqual(rows, ALL_TITLES)


class Gate1NextStepRenderingTests(unittest.TestCase):
    """The steps appear in text and Markdown reports, never in JSON."""

    def test_text_and_markdown_blocks(self) -> None:
        """Both formats give the status, numbered titles and labelled lines."""
        payload = json.loads(
            (EXAMPLES / "phantom-tier-framework-hit.json").read_text(encoding="utf-8")
        )
        assessment = assess_evidence(payload)
        steps = gate1_next_steps(payload, assessment)
        text = render_text(assessment, None, steps)
        self.assertIn("\n\nNext steps — Gate 1 FAIL\n1. Tier device read nothing\n", text)
        self.assertIn("   Observed: 3/3 target-tier requests", text)
        self.assertIn("   Check: whether the entries were written", text)
        self.assertIn("\n2. Counter not filtered to the tier's device\n   Missing:", text)
        markdown = render_markdown(assessment, None, steps)
        self.assertIn(
            "\n## Next steps — Gate 1 FAIL\n\n1. **Tier device read nothing**\n", markdown
        )
        self.assertIn(", `docs/FAILURE-CATALOG.md#staging-buffer-full`\n2. **", markdown)

    def test_library_rendering_is_unchanged_without_steps(self) -> None:
        """Callers that pass no steps get the report they got before."""
        payload = json.loads(
            (EXAMPLES / "attribution-unfiltered-counter.json").read_text(encoding="utf-8")
        )
        assessment = assess_evidence(payload)
        for render in (render_text, render_markdown):
            with self.subTest(render=render.__name__):
                self.assertNotIn("Next steps", render(assessment))
                self.assertEqual(render(assessment), render(assessment, None, ()))

    def test_cli_formats(self) -> None:
        """text and markdown add the steps; JSON is the assessment unchanged."""
        path = EXAMPLES / "attribution-unfiltered-counter.json"
        assessment = assess_evidence(json.loads(path.read_text(encoding="utf-8")))
        outputs = {}
        for fmt in ("text", "markdown", "json"):
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = main(["report", str(path), "--format", fmt, "--no-readings"])
            self.assertEqual(code, 1)
            outputs[fmt] = stdout.getvalue()
        self.assertIn("Next steps — Gate 1 UNPROVEN", outputs["text"])
        self.assertIn("## Next steps — Gate 1 UNPROVEN", outputs["markdown"])
        self.assertNotIn("Next steps", outputs["json"])
        self.assertEqual(json.loads(outputs["json"]), assessment)

    def test_first_visit_reports(self) -> None:
        """Bundled non-PASS reports carry the steps; the PASS report does not."""
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            output = Path(directory) / "first-visit"
            main(["first-visit", "--output-dir", str(output)])
            unfiltered = (output / "attribution-unfiltered-counter.report.md").read_text(
                encoding="utf-8"
            )
            phantom = (output / "phantom-tier-framework-hit.report.md").read_text(encoding="utf-8")
            passing = (output / "pass-all-attempts.report.md").read_text(encoding="utf-8")
        self.assertIn("## Next steps — Gate 1 UNPROVEN", unfiltered)
        self.assertIn("## Next steps — Gate 1 FAIL", phantom)
        self.assertNotIn("Next steps", passing)


if __name__ == "__main__":
    unittest.main()
