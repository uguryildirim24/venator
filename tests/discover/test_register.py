"""Turning a pasted URL into a board token, without any live HTTP.

Every fetch in these tests is a canned document handed to ``resolve`` through
its reader seam, and every probe is a stubbed adapter, so what is asserted is
this repo's resolution and its refusals — not any board's uptime.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from venator.discover import register
from venator.discover.harvest import extract_board_tokens
from venator.discover.register import (
    Board,
    Confirmation,
    RegisterError,
    board_from_url,
    confirm,
    normalize_url,
    page_title,
    record,
    resolve,
    run,
    sites_from_robots,
    workday_tenant_hosts,
    yaml_lines,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAREERS_URL = "https://careers.examplepharma.example/en/"
TENANT = "examplepharma.wd1.myworkdayjobs.com"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _reader(pages: dict[str, str]):
    """A stand-in for the network: every URL must be one this test canned."""

    def read(url: str) -> str:
        if url not in pages:
            raise AssertionError(f"unexpected fetch: {url}")
        return pages[url]

    return read


CAREERS_PAGES = {
    CAREERS_URL: _fixture("careers_page.html"),
    f"https://{TENANT}/robots.txt": _fixture("workday_robots.txt"),
}


class _Profile:
    """The only two things ``run`` asks a Profile for."""

    def __init__(self, boards: dict[str, list[str]] | None = None) -> None:
        self.sources = type("Sources", (), {"boards": boards or {}})()
        self.targeting_path = Path("profiles/example/targeting.yaml")


class UrlShapeTests(unittest.TestCase):
    def test_every_accepted_direct_board_url_shape(self) -> None:
        cases = {
            "https://job-boards.greenhouse.io/Example_Bio/jobs/123": ("greenhouse", "Example_Bio"),
            "https://boards.greenhouse.io/second-biotech?gh_jid=456": ("greenhouse", "second-biotech"),
            "https://boards.greenhouse.io/embed/job_board?for=embedded-bio": ("greenhouse", "embedded-bio"),
            "https://boards-api.greenhouse.io/v1/boards/api_board/jobs": ("greenhouse", "api_board"),
            "https://jobs.lever.co/lever-labs/abc-def": ("lever", "lever-labs"),
            "https://jobs.eu.lever.co/eu-labs": ("lever", "eu-labs"),
            "https://api.lever.co/v0/postings/api-lever?mode=json": ("lever", "api-lever"),
            "https://jobs.ashbyhq.com/ashby-labs/uuid": ("ashby", "ashby-labs"),
            "https://api.ashbyhq.com/posting-api/job-board/api_ashby": ("ashby", "api_ashby"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(board_from_url(url), [expected])

    def test_every_accepted_workday_url_shape_gives_the_two_part_token(self) -> None:
        cases = {
            "https://amgen.wd1.myworkdayjobs.com/Careers": "amgen.wd1~Careers",
            "https://amgen.wd1.myworkdayjobs.com/en-US/Careers": "amgen.wd1~Careers",
            "https://amgen.wd1.myworkdayjobs.com/en-US/Careers/": "amgen.wd1~Careers",
            "https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/Remote/Analyst_R42": (
                "pfizer.wd1~PfizerCareers"
            ),
            "https://nvidia.wd5.myworkdayjobs.com/fr-FR/NVIDIAExternalCareerSite": (
                "nvidia.wd5~NVIDIAExternalCareerSite"
            ),
            "https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers/jobs": "amgen.wd1~Careers",
        }
        for url, token in cases.items():
            with self.subTest(url=url):
                self.assertEqual(board_from_url(url), [("workday", token)])

    def test_a_workday_host_prefix_is_lowercased_and_the_site_string_is_not(self) -> None:
        # The host half is a hostname and case-insensitive; the site half is a
        # path segment the employer chose, and `Careers` is not `careers`.
        self.assertEqual(
            board_from_url("https://Amgen.WD1.myworkdayjobs.com/en-US/Careers"),
            [("workday", "amgen.wd1~Careers")],
        )

    def test_a_greenhouse_token_keeps_its_case(self) -> None:
        self.assertEqual(
            board_from_url("https://job-boards.greenhouse.io/Example_Bio"),
            [("greenhouse", "Example_Bio")],
        )

    def test_a_bare_workday_tenant_host_names_no_board_on_its_own(self) -> None:
        # No site string is in the URL, so there is nothing to guess from; the
        # tenant's robots.txt is what answers it.
        self.assertEqual(board_from_url(f"https://{TENANT}/"), [])


class UntrustedUrlTests(unittest.TestCase):
    def test_a_non_http_scheme_is_refused(self) -> None:
        for url in ("javascript:https://jobs.lever.co/acme", "data:text/html,x", "careers.acme.example"):
            with self.subTest(url=url):
                with self.assertRaises(RegisterError) as caught:
                    normalize_url(url)
                self.assertIn("http(s)", str(caught.exception))

    def test_a_lookalike_hostname_is_not_a_board(self) -> None:
        # A host that merely contains an ATS domain, on either side of it.
        lookalikes = (
            "https://greenhouse.io.evil.example/acme",
            "https://boards.greenhouse.io.evil.example/acme",
            "https://notboards.greenhouse.io/acme",
            "https://jobs.lever.co.evil.example/acme",
            "https://myjobs.lever.co/acme",
            "https://jobs.ashbyhq.com.evil.example/acme",
            "https://examplepharma.wd1.myworkdayjobs.com.evil.example/Careers",
            "https://evil.example/boards.greenhouse.io/acme",
        )
        for url in lookalikes:
            with self.subTest(url=url):
                self.assertEqual(board_from_url(url), [])
                self.assertEqual(extract_board_tokens(url), set())

    def test_a_url_that_only_mentions_a_board_is_not_that_board(self) -> None:
        # Anchored at position 0: the pasted URL has to *be* the board URL.
        # `extract_board_tokens` searches anywhere, which is right for a Posting
        # description and wrong for a paste.
        wrapped = "https://elsewhere.example/?next=https://jobs.lever.co/acme"
        self.assertEqual(board_from_url(wrapped), [])
        self.assertEqual(extract_board_tokens(wrapped), {("lever", "acme")})

    def test_credentials_before_the_host_are_refused(self) -> None:
        with self.assertRaises(RegisterError) as caught:
            normalize_url("https://boards.greenhouse.io@evil.example/acme")
        self.assertIn("credentials", str(caught.exception))

    def test_a_bare_address_or_this_machine_is_refused(self) -> None:
        for url in ("http://127.0.0.1:8080/x", "http://localhost/x", "http://[::1]/x"):
            with self.subTest(url=url):
                with self.assertRaises(RegisterError):
                    normalize_url(url)

    def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        redirect = httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
        with patch.object(httpx, "get", return_value=redirect):
            with self.assertRaises(RegisterError) as caught:
                register.read_url(CAREERS_URL)
        self.assertIn("Redirects are not followed", str(caught.exception))
        self.assertIn("elsewhere.example", str(caught.exception))

    def test_the_fetch_pins_the_host_it_was_aimed_at(self) -> None:
        with patch.object(httpx, "get", side_effect=RuntimeError("stop")) as get:
            with self.assertRaises(RuntimeError):
                register.read_url(CAREERS_URL)
        self.assertIs(get.call_args.kwargs["follow_redirects"], False)
        self.assertEqual(get.call_args.args[0], CAREERS_URL)


class CareersPageTests(unittest.TestCase):
    def test_a_careers_page_resolves_through_its_tenant_to_robots_txt(self) -> None:
        boards, title = resolve(CAREERS_URL, _reader(CAREERS_PAGES))

        self.assertEqual(
            [(board.source, board.board) for board in boards],
            [
                ("ashby", "examplepharma-ventures"),
                ("greenhouse", "examplepharmalabs"),
                ("workday", "examplepharma.wd1~ExampleCareers"),
                ("workday", "examplepharma.wd1~EarlyCareers"),
            ],
        )
        self.assertEqual(title, "Careers & Culture | Example Pharma")
        self.assertEqual(
            [board.route for board in boards if board.source == "workday"],
            [f"{TENANT}/robots.txt"] * 2,
        )

    def test_a_lookalike_tenant_host_on_the_page_is_not_a_tenant(self) -> None:
        page = _fixture("careers_page.html")
        self.assertEqual(workday_tenant_hosts(page), [TENANT])
        self.assertNotIn("evil", " ".join(workday_tenant_hosts(page)))

    def test_robots_txt_enumerates_the_site_strings_and_pins_the_tenant(self) -> None:
        sites = sites_from_robots(TENANT, _fixture("workday_robots.txt"))

        # From `Sitemap:` and `Allow:`, with the locale segment dropped, `wday`
        # excluded, wildcard patterns ignored, and another tenant's sitemap left
        # to that tenant.
        self.assertEqual(sites, ["ExampleCareers", "EarlyCareers"])

    def test_a_robots_txt_naming_too_many_sites_is_trimmed_out_loud(self) -> None:
        # Every site string costs a listing request to confirm, so the walk is
        # capped — and the cap says so rather than quietly shortening the answer.
        robots = "\n".join(
            f"Allow: /en-US/Site{index}" for index in range(register.SITES_PER_TENANT + 3)
        )
        with redirect_stdout(io.StringIO()) as out:
            boards, _ = resolve(
                f"https://{TENANT}/", _reader({f"https://{TENANT}/robots.txt": robots})
            )

        self.assertEqual(len(boards), register.SITES_PER_TENANT)
        self.assertIn(f"probing the first {register.SITES_PER_TENANT}", out.getvalue())

    def test_a_bare_tenant_host_skips_the_page_and_asks_robots_txt(self) -> None:
        boards, title = resolve(
            f"https://{TENANT}/",
            _reader({f"https://{TENANT}/robots.txt": _fixture("workday_robots.txt")}),
        )

        self.assertEqual(
            [board.board for board in boards],
            ["examplepharma.wd1~ExampleCareers", "examplepharma.wd1~EarlyCareers"],
        )
        self.assertIsNone(title)

    def test_a_page_naming_no_board_is_an_error_that_says_what_to_do(self) -> None:
        with self.assertRaises(RegisterError) as caught:
            resolve(CAREERS_URL, _reader({CAREERS_URL: "<html><body>no links</body></html>"}))

        self.assertIn("paste that Posting's URL instead", str(caught.exception))

    def test_a_page_title_is_a_hint_and_never_a_display_name(self) -> None:
        self.assertEqual(page_title("<title>  Careers &amp;\n Jobs </title>"), "Careers & Jobs")
        self.assertIsNone(page_title("<html></html>"))


class ConfirmationTests(unittest.TestCase):
    def test_an_unconfirmed_board_is_reported_and_never_dropped(self) -> None:
        boards = [
            Board("greenhouse", "answers", CAREERS_URL, "careers page"),
            Board("workday", "examplepharma.wd1~EarlyCareers", CAREERS_URL, "robots.txt"),
        ]
        adapters = {
            "greenhouse": lambda board: [{"key": "a"}, {"key": "b"}],
            "workday": lambda board: (_ for _ in ()).throw(RuntimeError("first page had no postings")),
        }
        with patch.dict(register.ADAPTERS, adapters, clear=True):
            with redirect_stdout(io.StringIO()) as out:
                results = confirm(boards)

        self.assertEqual([result.confirmed for result in results], [True, False])
        self.assertEqual(results[0].posting_count, 2)
        self.assertIn("first page had no postings", results[1].detail)
        self.assertIn("not confirmed", out.getvalue())
        self.assertIn("confirmed (2 Postings", out.getvalue())


class OutputTests(unittest.TestCase):
    def _results(self) -> list[Confirmation]:
        return [
            Confirmation(Board("greenhouse", "examplepharmalabs", CAREERS_URL, "careers page"), True, 7, ""),
            Confirmation(
                Board("workday", "examplepharma.wd1~ExampleCareers", CAREERS_URL, "robots.txt"), True, 214, ""
            ),
            Confirmation(Board("ashby", "dead-board", CAREERS_URL, "careers page"), False, 0, "404"),
        ]

    def test_the_printed_yaml_carries_both_boards_and_names(self) -> None:
        lines = yaml_lines(
            self._results(),
            {"examplepharmalabs": "Example Pharma Labs", "examplepharma.wd1~ExampleCareers": "Example Pharma"},
        )

        self.assertEqual(
            lines,
            [
                "  names:",
                "    examplepharmalabs: Example Pharma Labs",
                "    examplepharma.wd1~ExampleCareers: Example Pharma",
                "  boards:",
                "    greenhouse:",
                "      - examplepharmalabs # Example Pharma Labs — 7 Postings when confirmed",
                "    workday:",
                "      - examplepharma.wd1~ExampleCareers # Example Pharma — 214 Postings when confirmed",
            ],
        )
        # The unconfirmed board is nowhere in what the Owner would paste.
        self.assertNotIn("dead-board", "\n".join(lines))

    def test_the_printed_yaml_parses_back_into_the_registry_shape(self) -> None:
        lines = yaml_lines(self._results(), {"examplepharmalabs": "Example Pharma Labs"})
        parsed = yaml.safe_load("sources:\n" + "\n".join(lines))

        self.assertEqual(parsed["sources"]["boards"]["greenhouse"], ["examplepharmalabs"])
        self.assertEqual(parsed["sources"]["boards"]["workday"], ["examplepharma.wd1~ExampleCareers"])
        self.assertEqual(parsed["sources"]["names"]["examplepharmalabs"], "Example Pharma Labs")
        self.assertEqual(
            parsed["sources"]["names"]["examplepharma.wd1~ExampleCareers"], register.NAME_PLACEHOLDER
        )

    def test_an_unnamed_employer_is_a_placeholder_the_output_explains(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            register.report(self._results(), {}, _Profile(), set(), {CAREERS_URL: "Example Pharma"})
        printed = out.getvalue()

        self.assertIn(register.NAME_PLACEHOLDER, printed)
        self.assertIn("filters_version", printed)
        self.assertIn("unconfirmed, so nothing to add: ashby:dead-board", printed)
        self.assertIn("page title at", printed)

    def test_an_already_registered_board_is_said_so_and_not_offered_again(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            register.report(
                self._results(), {}, _Profile(), {("greenhouse", "examplepharmalabs")}, {}
            )
        printed = out.getvalue()

        self.assertIn("already registered", printed)
        self.assertNotIn("- examplepharmalabs #", printed)


class RecordTests(unittest.TestCase):
    def test_recording_merges_into_the_suggestions_file_and_touches_no_profile(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board-suggestions.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "generated_at": "2026-08-01T00:00:00+00:00",
                        "suggestions": [
                            {"source": "lever", "board": "harvested", "company": "Harvested", "posting_count": 3}
                        ],
                    }
                )
            )
            results = [
                Confirmation(Board("workday", "examplepharma.wd1~ExampleCareers", "u", "r"), True, 214, ""),
                Confirmation(Board("ashby", "dead-board", "u", "r"), False, 0, "404"),
            ]

            suggestions = record(results, {"examplepharma.wd1~ExampleCareers": "Example Pharma"}, path)

            self.assertEqual(
                suggestions,
                [
                    {"source": "lever", "board": "harvested", "company": "Harvested", "posting_count": 3},
                    {
                        "source": "workday",
                        "board": "examplepharma.wd1~ExampleCareers",
                        "company": "Example Pharma",
                        "posting_count": 214,
                    },
                ],
            )
            self.assertEqual(yaml.safe_load(path.read_text())["suggestions"], suggestions)


class RunTests(unittest.TestCase):
    def test_one_paste_end_to_end_prints_the_lines_to_add(self) -> None:
        adapters = {
            "greenhouse": lambda board: [{"key": "a"}],
            "ashby": lambda board: [],
            "workday": lambda board: [{"key": str(index)} for index in range(5)],
        }
        with patch.dict(register.ADAPTERS, adapters, clear=True):
            with redirect_stdout(io.StringIO()) as out:
                results = run(
                    [CAREERS_URL],
                    _Profile({"greenhouse": ["examplepharmalabs"]}),
                    "Example Pharma",
                    None,
                    _reader(CAREERS_PAGES),
                )
        printed = out.getvalue()

        self.assertEqual(len(results), 4)
        self.assertIn("      - examplepharma.wd1~ExampleCareers # Example Pharma", printed)
        self.assertIn("    examplepharma.wd1~ExampleCareers: Example Pharma", printed)
        # Already in the Profile, so it is named as such rather than re-offered.
        self.assertIn("greenhouse:examplepharmalabs: already registered", printed)

    def test_a_refused_url_does_not_stop_the_rest_of_the_paste(self) -> None:
        with patch.dict(register.ADAPTERS, {"lever": lambda board: [{"key": "a"}]}, clear=True):
            with redirect_stdout(io.StringIO()) as out:
                results = run(
                    ["javascript:alert(1)", "https://jobs.lever.co/lever-labs"],
                    _Profile(),
                    None,
                    None,
                    _reader({}),
                )
        printed = out.getvalue()

        self.assertEqual([result.board.board for result in results], ["lever-labs"])
        self.assertIn("is not an http(s) URL", printed)


if __name__ == "__main__":
    unittest.main()
