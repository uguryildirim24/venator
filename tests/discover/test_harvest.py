from __future__ import annotations

import unittest

from venator.discover.harvest import extract_board_tokens, find_candidates


class HarvestTests(unittest.TestCase):
    def test_extracts_tokens_from_public_and_api_urls(self) -> None:
        text = """
        https://job-boards.greenhouse.io/Example_Bio/jobs/123
        https://boards.greenhouse.io/second-biotech?gh_jid=456
        https://boards.greenhouse.io/embed/job_board?for=embedded-biotech
        https://boards-api.greenhouse.io/v1/boards/api_board/jobs
        https://jobs.lever.co/lever-labs/abc-def
        https://jobs.eu.lever.co/eu-labs/abc-def
        https://api.lever.co/v0/postings/api-lever?mode=json
        https://jobs.ashbyhq.com/ashby-labs/uuid
        https://api.ashbyhq.com/posting-api/job-board/api_ashby
        https://jobs.smartrecruiters.com/BicycleTherapeutics/7440001-role
        https://api.smartrecruiters.com/v1/companies/ExamplePharma/postings
        """

        self.assertEqual(
            extract_board_tokens(text),
            {
                ("greenhouse", "Example_Bio"),
                ("greenhouse", "second-biotech"),
                ("greenhouse", "embedded-biotech"),
                ("greenhouse", "api_board"),
                ("lever", "lever-labs"),
                ("lever", "eu-labs"),
                ("lever", "api-lever"),
                ("ashby", "ashby-labs"),
                ("ashby", "api_ashby"),
                ("smartrecruiters", "BicycleTherapeutics"),
                ("smartrecruiters", "ExamplePharma"),
            },
        )

    def test_extracts_html_escaped_url_and_company(self) -> None:
        postings = [
            {
                "url": "https://example.com/redirect",
                "description_html": (
                    "Apply at https://jobs.lever.co/examplebio/123?foo=1&amp;bar=2"
                ),
                "company": "Example Bio",
            }
        ]

        self.assertEqual(
            find_candidates(postings),
            {("lever", "examplebio"): "Example Bio"},
        )


if __name__ == "__main__":
    unittest.main()
