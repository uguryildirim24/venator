"""Workday adapter: normalization, keying, paging, and the failures that must be loud.

No live HTTP. The listing and detail documents are canned fixtures written to the
real response shape, and the paging tests drive the pure paging loop through a
stub page fetcher, so what is asserted here is this repo's mapping and not
Workday's uptime.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from venator.discover import adapters as discover_adapters
from venator.discover.adapters import (
    ADAPTERS,
    ENRICHERS,
    ExactJobClosed,
    WORKDAY_PAGE_LIMIT,
    WorkdayError,
    enrich_workday,
    fetch_workday,
    parse_workday_board as _parse,
    normalize_workday_detail,
    normalize_workday_listing,
    parse_workday_board,
    workday_listing_body,
    workday_pages,
)

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = "amgen.wd1~Careers"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class WorkdayTokenTests(unittest.TestCase):
    def test_token_splits_into_host_tenant_and_site(self) -> None:
        # The host carries the wd<N> instance; the cxs path carries the bare tenant.
        self.assertEqual(
            parse_workday_board(BOARD),
            ("amgen.wd1.myworkdayjobs.com", "amgen", "Careers"),
        )
        self.assertEqual(
            parse_workday_board("nvidia.wd5~NVIDIAExternalCareerSite"),
            ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite"),
        )

    def test_malformed_tokens_are_refused_by_name(self) -> None:
        malformed = [
            "amgen",  # no site at all
            "amgen.wd1",  # tenant only
            "amgen.wd1~",  # empty site
            "~Careers",  # empty tenant
            "amgen~Careers",  # no wd<N> instance
            "amgen.wd1:Careers",  # a colon would collide with the Posting key separator
            "amgen.wd1/Careers",  # a slash would collide with the dashboard's API path
            "",
        ]
        for token in malformed:
            with self.subTest(token=token):
                with self.assertRaises(WorkdayError) as caught:
                    parse_workday_board(token)
                # A clear error, not a KeyError-shaped one, and it shows the shape.
                self.assertIn("<tenant>.wd<N>~<site>", str(caught.exception))
                self.assertIn(repr(token), str(caught.exception))


class WorkdayListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.postings = normalize_workday_listing(BOARD, _fixture("workday_listing"))

    def test_normalizes_canned_listing(self) -> None:
        self.assertEqual(len(self.postings), 3)
        posting = self.postings[0]
        self.assertEqual(posting["key"], "workday:amgen.wd1~Careers:R-192837")
        self.assertEqual(posting["source"], "workday")
        self.assertEqual(posting["board"], BOARD)
        self.assertEqual(posting["title"], "Summer 2027 Intern - Process Development")
        self.assertEqual(posting["location"], "US - Cambridge, MA")
        self.assertEqual(
            posting["url"],
            "https://amgen.wd1.myworkdayjobs.com/Careers"
            "/job/US---Cambridge-MA/Summer-2027-Intern---Process-Development_R-192837",
        )

    def test_keys_on_the_requisition_id_and_never_on_the_url_slug(self) -> None:
        # The slug embeds the job title, and data/ is append-only: keying on it
        # would re-discover the same job as a brand-new Posting on every retitle.
        self.assertEqual([p["external_id"] for p in self.postings], ["R-192837", "R-104556", "R-100001"])
        for posting in self.postings:
            self.assertNotIn(posting["external_id"], (posting["external_path"], ""))
            self.assertNotIn("Intern", posting["external_id"])
            self.assertNotIn("Scientist", posting["external_id"])

    def test_a_retitled_posting_keeps_its_key(self) -> None:
        retitled = _fixture("workday_listing")
        entry = retitled["jobPostings"][0]
        entry["title"] = "Summer 2027 Co-op - Process Development"
        entry["externalPath"] = "/job/US---Cambridge-MA/Summer-2027-Co-op---Process-Development_R-192837"

        self.assertEqual(normalize_workday_listing(BOARD, retitled)[0]["key"], self.postings[0]["key"])

    def test_posted_at_is_the_start_date_not_the_posted_on_prose(self) -> None:
        self.assertEqual([p["posted_at"] for p in self.postings], ["2026-08-17", "2026-08-12", "2026-07-04"])
        for posting in self.postings:
            self.assertNotIn("Posted", posting["posted_at"])

    def test_additional_locations_are_optional_and_deduplicated_when_present(self) -> None:
        self.assertEqual(self.postings[0]["location"], "US - Cambridge, MA")
        self.assertEqual(self.postings[1]["location"], "US - Thousand Oaks, CA; US - Cambridge, MA")

    def test_an_entry_without_a_requisition_id_is_refused(self) -> None:
        data = {"jobPostings": [{"title": "Scientist", "externalPath": "/job/X/Scientist_R-1", "bulletFields": []}]}

        with self.assertRaises(WorkdayError) as caught:
            normalize_workday_listing(BOARD, data)

        self.assertIn("bulletFields", str(caught.exception))


class WorkdayDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.listing = normalize_workday_listing(BOARD, _fixture("workday_listing"))

    def test_detail_completes_the_posting(self) -> None:
        posting = normalize_workday_detail(self.listing[1], _fixture("workday_detail"))

        self.assertEqual(posting["key"], "workday:amgen.wd1~Careers:R-104556")
        self.assertEqual(posting["external_id"], "R-104556")
        self.assertIn("release and stability assays", posting["description_html"])
        self.assertIn("not eligible for visa sponsorship", posting["description_html"])
        # The canonical apply URL is returned outright; it is never constructed.
        self.assertEqual(
            posting["url"],
            "https://amgen.wd1.myworkdayjobs.com/en-US/Careers"
            "/job/US---Thousand-Oaks-CA/Associate-Scientist--Analytical-Sciences_R-104556",
        )
        self.assertEqual(posting["posted_at"], "2026-08-12")
        self.assertEqual(posting["location"], "US - Thousand Oaks, CA; US - Cambridge, MA")
        self.assertEqual(posting["discovered_at"], self.listing[1]["discovered_at"])

    def test_a_completed_posting_carries_a_description_and_no_snippet_marker(self) -> None:
        # A Posting with no description is snippet-marked and reaches assessment
        # with nothing to read. Completing it before it is appended is the whole point of
        # the detail fetch, so an empty description here would be a regression.
        posting = normalize_workday_detail(self.listing[0], _fixture("workday_detail_minimal"))

        self.assertNotEqual(posting["description_html"], "")
        self.assertNotIn("snippet", posting)

    def test_the_external_path_survives_detail_normalization_for_refresh(self) -> None:
        posting = normalize_workday_detail(self.listing[0], _fixture("workday_detail_minimal"))

        self.assertIn("external_path", self.listing[0])
        self.assertEqual(posting["external_path"], self.listing[0]["external_path"])

    def test_company_never_comes_from_the_hiring_organization(self) -> None:
        # hiringOrganization.name answers with an internal entity code — "2100
        # Amgen USA" — not the employer. The employer display name is the
        # Profile's sources.names, exactly as for the other ATS adapters.
        detail = _fixture("workday_detail")
        posting = normalize_workday_detail(self.listing[1], detail)

        self.assertNotIn("company", posting)
        self.assertNotIn(detail["hiringOrganization"]["name"], json.dumps(posting))

    def test_end_date_and_remote_type_are_preserved_when_present(self) -> None:
        full = normalize_workday_detail(self.listing[1], _fixture("workday_detail"))
        minimal = normalize_workday_detail(self.listing[0], _fixture("workday_detail_minimal"))

        # These optional facts used to be discarded.  They now remain visible
        # when a tenant supplies them, while a tenant that omits them still
        # normalizes successfully.
        self.assertEqual(full["remote"], "On-site")
        self.assertEqual(full["remote_type"], "On-site")
        self.assertEqual(full["endDate"], "2026-09-30")
        self.assertEqual(full["application_deadline"], "2026-09-30")
        self.assertEqual(full["workplace_type"], "onsite")
        self.assertNotIn("remote", minimal)
        self.assertNotIn("endDate", minimal)

    def test_a_different_detail_requisition_is_refused(self) -> None:
        detail = _fixture("workday_detail")
        detail["jobPostingInfo"]["jobReqId"] = "R-104556-B"
        with self.assertRaisesRegex(WorkdayError, "does not match requested"):
            normalize_workday_detail(self.listing[1], detail)

    def test_a_detail_document_without_job_posting_info_is_refused(self) -> None:
        with self.assertRaises(WorkdayError) as caught:
            normalize_workday_detail(self.listing[0], {"userAuthenticated": False})

        self.assertIn("jobPostingInfo", str(caught.exception))

    def test_a_posting_without_a_path_or_canonical_url_is_not_fetched(self) -> None:
        posting = dict(self.listing[0], external_path="", url="")
        with patch.object(httpx, "get") as get:
            with self.assertRaises(WorkdayError):
                enrich_workday(posting)
        get.assert_not_called()


class _StubListing:
    """A canned listing endpoint: records what was asked for, answers page sizes."""

    def __init__(self, page_sizes: list[int]) -> None:
        self.page_sizes = page_sizes
        self.calls: list[tuple[int, int]] = []
        self._next = 0

    def __call__(self, limit: int, offset: int) -> dict[str, Any]:
        self.calls.append((limit, offset))
        size = self.page_sizes[min(self._next, len(self.page_sizes) - 1)]
        self._next += 1
        start = self._next * 1000
        return {
            "total": sum(self.page_sizes),
            "jobPostings": [
                {
                    "title": f"Scientist {start + index}",
                    "externalPath": f"/job/US---Cambridge-MA/Scientist_R-{start + index}",
                    "locationsText": "US - Cambridge, MA",
                    "postedOn": "Posted Today",
                    "startDate": "2026-08-17",
                    "bulletFields": [f"R-{start + index}"],
                }
                for index in range(size)
            ]
        }


class WorkdayPagingTests(unittest.TestCase):
    def test_listing_body_sends_all_four_keys_the_real_client_sends(self) -> None:
        self.assertEqual(
            workday_listing_body(WORKDAY_PAGE_LIMIT, 40),
            {"appliedFacets": {}, "limit": 20, "offset": 40, "searchText": ""},
        )

    def test_page_size_is_capped_here_rather_than_relied_on_there(self) -> None:
        # Whether the endpoint clamps an over-large limit is not known, so the
        # cap is applied before the request, not assumed after it.
        self.assertEqual(workday_listing_body(500, 0)["limit"], WORKDAY_PAGE_LIMIT)
        self.assertEqual(workday_listing_body(0, 0)["limit"], 1)
        self.assertEqual(workday_listing_body(20, -5)["offset"], 0)

        listing = _StubListing([WORKDAY_PAGE_LIMIT, 3])
        workday_pages(BOARD, listing, limit=500)

        self.assertEqual([limit for limit, _offset in listing.calls], [WORKDAY_PAGE_LIMIT] * 2)

    def test_pagination_walks_by_offset_and_stops_on_a_short_page(self) -> None:
        listing = _StubListing([WORKDAY_PAGE_LIMIT, WORKDAY_PAGE_LIMIT, 7])

        postings = workday_pages(BOARD, listing)

        self.assertEqual(len(postings), 47)
        self.assertEqual([offset for _limit, offset in listing.calls], [0, 20, 40])

    def test_an_explicit_interactive_page_limit_reports_a_partial_window(self) -> None:
        listing = _StubListing([WORKDAY_PAGE_LIMIT] * 4)
        output = io.StringIO()

        with redirect_stdout(output):
            postings = workday_pages(BOARD, listing, max_pages=3)

        self.assertEqual(len(listing.calls), 3)
        self.assertEqual(len(postings), 3 * WORKDAY_PAGE_LIMIT)
        self.assertFalse(postings.complete)
        self.assertIn("incomplete", output.getvalue())

    def test_repeated_keys_across_pages_are_collapsed(self) -> None:
        # Offset paging over a listing that is being edited under the walk can
        # hand back a Posting a previous page already carried.
        first = _fixture("workday_listing")
        first["total"] = 5
        overlap = {"total": 5, "jobPostings": [first["jobPostings"][2], dict(first["jobPostings"][0], bulletFields=["R-999"])]}
        pages = [first, overlap]
        calls: list[int] = []

        def fetch_page(limit: int, offset: int) -> dict[str, Any]:
            calls.append(offset)
            return pages[min(len(calls) - 1, len(pages) - 1)]

        postings = workday_pages(BOARD, fetch_page, limit=3)

        self.assertEqual([p["external_id"] for p in postings], ["R-192837", "R-104556", "R-100001", "R-999"])
        self.assertEqual(calls, [0, 3])

    def test_offset_at_reported_total_completes_before_wraparound_rows_stall_it(self) -> None:
        wrapped = _fixture("workday_listing")
        wrapped["total"] = 117
        result = workday_pages(
            "qiagen.wd502~QIAGEN",
            lambda limit, offset: wrapped,
            start_offset=120,
            resumable=True,
        )
        self.assertTrue(result.scan_complete)
        self.assertFalse(result.stalled)
        self.assertEqual(list(result), [])

    def test_an_empty_first_page_with_zero_total_is_a_valid_empty_board(self) -> None:
        result = workday_pages(
            BOARD, lambda limit, offset: {"total": 0, "jobPostings": []},
        )
        self.assertEqual(list(result), [])
        self.assertTrue(result.complete)
        self.assertTrue(result.scan_complete)

    def test_a_listing_without_total_is_refused(self) -> None:
        with self.assertRaisesRegex(WorkdayError, "valid total"):
            workday_pages(BOARD, lambda limit, offset: {"jobPostings": []})

    def test_a_board_without_identifiers_is_incomplete(self) -> None:
        page = _fixture("workday_listing")
        for entry in page["jobPostings"]:
            entry["bulletFields"] = ["Full time"]
        result = workday_pages(BOARD, lambda limit, offset: page)
        self.assertEqual(list(result), [])
        self.assertFalse(result.complete)
        self.assertEqual(result.status, "partial")

    def test_identity_is_not_the_first_display_field(self) -> None:
        page = {"total": 1, "jobPostings": [{
            "title": "Intern, Applied Technologies (Summer 2027)",
            "externalPath": "/job/Norwood-Massachusetts/Intern--Applied-Technologies_R19734",
            "bulletFields": ["Norwood, Massachusetts", "Technical Development", "R19734"],
        }]}
        result = workday_pages("modernatx.wd1~M_tx", lambda limit, offset: page)
        self.assertEqual(result[0]["external_id"], "R19734")
        self.assertTrue(result.complete)

    def test_ambiguous_ids_require_url_corroboration(self) -> None:
        page = {"total": 1, "jobPostings": [{
            "title": "Scientist", "externalPath": "/job/Boston/Scientist_R-2",
            "bulletFields": ["12345", "R-2"],
        }]}
        self.assertEqual(normalize_workday_listing(BOARD, page)[0]["external_id"], "R-2")
        page["jobPostings"][0]["externalPath"] = "/job/Boston/Scientist"
        with self.assertRaises(WorkdayError):
            normalize_workday_listing(BOARD, page)

    def test_reported_total_stops_without_an_extra_empty_request(self) -> None:
        listing = _StubListing([WORKDAY_PAGE_LIMIT, 0])

        self.assertEqual(len(workday_pages(BOARD, listing)), WORKDAY_PAGE_LIMIT)
        self.assertEqual(len(listing.calls), 1)


class WorkdayRequestTests(unittest.TestCase):
    """What the two Workday call sites are allowed to put on the wire."""

    def test_the_listing_is_the_only_non_get_in_the_discover_path(self) -> None:
        # Workday's listing is a read-only job search, issued as a POST only
        # because the query is a JSON body. It is the one non-GET verb Discover
        # sends, and the never-submit guards — which live in the browser layer
        # and would abort it, correctly, being unable to tell a read POST from a
        # submit — are not in this path and must not be routed into it.
        package = Path(discover_adapters.__file__).parent
        modules = sorted(path for path in package.glob("*.py"))
        posts = 0
        for path in modules:
            source = path.read_text()
            for verb in (".put(", ".patch(", ".delete("):
                self.assertNotIn(verb, source, f"{path.name} issues a state-changing verb")
            posts += source.count(".post(")
        self.assertEqual(posts, 1)

    def test_neither_call_site_follows_a_redirect_off_the_tenant_host(self) -> None:
        # A followed 3xx would turn a discovery fetch into a request to whatever
        # server the endpoint named.
        with patch.object(httpx, "Client") as client:
            client.return_value.request.side_effect = RuntimeError("stop")
            with self.assertRaises(RuntimeError):
                fetch_workday(BOARD)
        self.assertIs(client.call_args.kwargs["follow_redirects"], False)

        with patch.object(httpx.Client, "request", side_effect=RuntimeError("stop")) as request:
            with self.assertRaises(RuntimeError):
                enrich_workday(normalize_workday_listing(BOARD, _fixture("workday_listing"))[0])
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertTrue(request.call_args.args[1].startswith(f"https://{_parse(BOARD)[0]}/wday/cxs/"))

    def test_s22_forbidden_detail_is_a_definitively_closed_posting(self) -> None:
        closed = httpx.Response(
            403,
            json={"errorCode": "S22", "httpStatus": 403, "message": "permission denied"},
            request=httpx.Request("GET", "https://example.invalid"),
        )
        with patch.object(httpx.Client, "request", return_value=closed):
            with self.assertRaises(ExactJobClosed):
                enrich_workday(normalize_workday_listing(BOARD, _fixture("workday_listing"))[0])

    def test_a_redirect_is_refused_rather_than_decoded_as_json(self) -> None:
        # raise_for_status() only raises on 4xx/5xx, so an unfollowed 3xx would
        # otherwise surface as a JSON decode error that explains nothing.
        redirect = httpx.Response(302, headers={"location": "https://elsewhere.invalid/x"})
        with patch.object(httpx.Client, "request", return_value=redirect):
            with self.assertRaises(WorkdayError) as caught:
                enrich_workday(normalize_workday_listing(BOARD, _fixture("workday_listing"))[0])

        self.assertIn("Redirects are not", str(caught.exception))
        self.assertIn("elsewhere.invalid", str(caught.exception))


class WorkdayRegistrationTests(unittest.TestCase):
    def test_workday_is_registered_as_a_source_and_as_an_enriched_one(self) -> None:
        self.assertIs(ADAPTERS["workday"], fetch_workday)
        self.assertIs(ENRICHERS["workday"], enrich_workday)


if __name__ == "__main__":
    unittest.main()


class WorkdaySourceLocationTests(unittest.TestCase):
    def test_moderna_location_bullet_is_corroborated_by_the_source_path(self):
        for requisition in ("R19734", "R19735"):
            with self.subTest(requisition=requisition):
                job = {"title": "Applied Technologies", "externalPath": f"/job/Norwood-Massachusetts/Applied-Technologies_{requisition}",
                       "bulletFields": ["Norwood, Massachusetts", "Technical Development", requisition]}
                posting = normalize_workday_listing("modernatx.wd1~M_tx", {"jobPostings": [job]})[0]
                self.assertEqual(posting["external_id"], requisition)
                self.assertEqual(posting["location"], "Norwood, Massachusetts")
                self.assertEqual(posting["locations"][0]["city"], "Norwood")
                self.assertEqual(posting["locations"][0]["region"], "Massachusetts")
                self.assertEqual(posting["locations"][0]["country"], "")

    def test_unlabeled_bullets_are_not_assumed_to_be_locations(self):
        job = {"title": "Scientist", "externalPath": "/job/Norwood-Massachusetts/Scientist_R1",
               "bulletFields": ["Technical Development", "R1"]}
        posting = normalize_workday_listing(BOARD, {"jobPostings": [job]})[0]
        self.assertEqual(posting["location"], "")
        self.assertEqual(posting["locations"], [])

    def test_explicit_listing_location_takes_precedence_over_bullets(self):
        job = {"title": "Scientist", "externalPath": "/job/Norwood-Massachusetts/Scientist_R1",
               "location": "Cambridge, MA", "bulletFields": ["Norwood, Massachusetts", "R1"]}
        posting = normalize_workday_listing(BOARD, {"jobPostings": [job]})[0]
        self.assertEqual(posting["location"], "Cambridge, MA")

    def test_lilly_country_first_location_uses_explicit_requisition_country(self):
        listing = normalize_workday_listing(BOARD, _fixture("workday_listing"))[0]
        result = normalize_workday_detail(listing, {"jobPostingInfo": {"jobReqId": listing["external_id"],
            "location": "Malaysia, Petaling Jaya", "country": {"descriptor": "Malaysia", "id": "opaque"},
            "jobRequisitionLocation": {"descriptor": "MY: GBS Kuala Lumpur",
                                       "country": {"descriptor": "Malaysia", "id": "opaque", "alpha2Code": "MY"}},
            "jobDescription": "Synthetic description.",
        }})
        self.assertEqual(result["locations"], [{"name": "Malaysia, Petaling Jaya", "country": "MY", "region": "", "city": "Petaling Jaya"}])
        self.assertEqual(result["location"], "Malaysia, Petaling Jaya")

    def test_roche_city_uses_country_metadata_not_city_guessing(self):
        listing = normalize_workday_listing(BOARD, _fixture("workday_listing"))[0]
        result = normalize_workday_detail(listing, {"jobPostingInfo": {"jobReqId": listing["external_id"],
            "location": "Petaling Jaya", "country": {"descriptor": "Malaysia", "id": "opaque"},
            "jobRequisitionLocation": {"descriptor": "Petaling Jaya", "country": {"alpha2Code": "MY"}},
        }})
        self.assertEqual(result["locations"][0]["country"], "MY")
        self.assertEqual(result["locations"][0]["city"], "Petaling Jaya")
        without_country = normalize_workday_detail(listing, {"jobPostingInfo": {"jobReqId": listing["external_id"], "location": "Petaling Jaya"}})
        self.assertEqual(without_country["locations"][0]["country"], "")

    def test_missing_detail_location_keeps_listing_places_and_enriches_explicit_country(self):
        listing = normalize_workday_listing(BOARD, {"jobPostings": [{
            "title": "Scientist", "externalPath": "/job/Petaling-Jaya/Scientist_R1", "bulletFields": ["R1"],
            "locationsText": "Petaling Jaya", "additionalLocations": ["Kuala Lumpur"],
        }]})[0]
        result = normalize_workday_detail(listing, {"jobPostingInfo": {"jobReqId": listing["external_id"],
            "country": {"descriptor": "Malaysia"}, "jobDescription": "Synthetic description.",
        }})
        self.assertEqual(result["location"], "Petaling Jaya; Kuala Lumpur")
        self.assertEqual([place["country"] for place in result["locations"]], ["MY", "MY"])
        self.assertEqual([place["city"] for place in result["locations"]], ["Petaling Jaya", "Kuala Lumpur"])

    def test_structured_location_alpha2_and_unknown_country_ids_are_distinct(self):
        listing = normalize_workday_listing(BOARD, _fixture("workday_listing"))[0]
        for country, expected in (({"alpha2Code": "MY", "id": "opaque"}, "MY"), ({"id": "opaque"}, "")):
            with self.subTest(country=country):
                result = normalize_workday_detail(listing, {"jobPostingInfo": {"jobReqId": listing["external_id"],
                    "location": {"name": "Petaling Jaya", "country": country},
                }})
                self.assertEqual(result["locations"][0]["country"], expected)


class WorkdayLocationAmbiguityTests(unittest.TestCase):
    def test_source_display_names_do_not_guess_ambiguous_countries(self):
        from venator.discover.common import normalize_location
        for label in ("Georgia", "Georgia, Atlanta", "Georgia - Atlanta", "IN", "IN, Indianapolis", "IN - Indianapolis", "Jamaica Plain, MA"):
            with self.subTest(label=label):
                self.assertEqual(normalize_location(label)["country"], "")
        self.assertEqual(normalize_location("Georgia", default_country="GE")["country"], "GE")
        self.assertEqual(normalize_location("IN", default_country="IN")["country"], "IN")
