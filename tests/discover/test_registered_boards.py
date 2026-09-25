"""Every board token a shipped Profile registers is well-formed, offline.

The tokens themselves were verified against the live boards by hand — a Workday
tenant is proved to exist by ``{tenant-host}/robots.txt`` answering 200 rather
than 422, and a Lever board by ``api.lever.co`` returning a non-empty array.
None of that is re-checked here: this suite has no network, and a sandbox that
cannot reach an employer would otherwise fail a token that is perfectly good.

What *is* checked is the half that a transcription slip breaks and no test
elsewhere covers — that each token parses the way the adapter that will fetch it
parses it, and that each one has an employer display name. The second is not
cosmetic: ``dedup.employer_display`` resolves an ATS Posting's employer through
``sources.names``, so a board registered without a name has no employer at all
and every Posting from it silently drops out of every duplicate group.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from venator.discover.adapters import ADAPTERS, WorkdayError, parse_workday_board
from venator.profile import load_profile

PROFILES = Path(__file__).resolve().parents[2] / "profiles"
PROFILE_NAMES = sorted(p.name for p in PROFILES.iterdir() if (p / "targeting.yaml").is_file())


class RegisteredBoardsTest(unittest.TestCase):
    def test_profiles_are_discovered(self) -> None:
        """The loop below is worthless if it silently iterates over nothing."""
        self.assertIn("example", PROFILE_NAMES)

    def test_every_workday_token_parses(self) -> None:
        """A slipped `~`, a missing wd<N>, or a stray space is caught here."""
        seen = 0
        for name in PROFILE_NAMES:
            registry = load_profile(PROFILES / name).sources
            for token in registry.boards.get("workday", ()):
                with self.subTest(profile=name, board=token):
                    try:
                        host, tenant, site = parse_workday_board(token)
                    except WorkdayError as error:  # pragma: no cover - failure path
                        self.fail(f"{name}: workday token {token!r} does not parse: {error}")
                    self.assertTrue(host.endswith(".myworkdayjobs.com"))
                    self.assertTrue(host.startswith(f"{tenant}."))
                    self.assertNotEqual(site, "")
                    # The token is round-tripped through a Posting key, whose
                    # separator is ':' and whose board half is a path parameter
                    # in the dashboard's API.
                    self.assertNotIn(":", token)
                    self.assertNotIn("/", token)
                    self.assertEqual(token, token.strip())
                    seen += 1

    def test_lever_tokens_are_url_safe_and_case_preserved(self) -> None:
        """Lever has no parser, so the shape a URL and a Posting key need is asserted.

        Case is deliberately not normalized: ``api.lever.co`` is case-sensitive
        and `Kyverna` and `LyciaTherapeutics` are the tokens as they really are.
        """
        seen = 0
        for name in PROFILE_NAMES:
            registry = load_profile(PROFILES / name).sources
            for token in registry.boards.get("lever", ()):
                with self.subTest(profile=name, board=token):
                    self.assertEqual(token, token.strip())
                    self.assertNotEqual(token, "")
                    self.assertRegex(token, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
                    self.assertNotIn(":", token)
                    self.assertNotIn("/", token)
                    seen += 1

    def test_every_registered_board_has_an_employer_display_name(self) -> None:
        """No entry in `sources.names` means no employer, which means no Dedup."""
        for name in PROFILE_NAMES:
            registry = load_profile(PROFILES / name).sources
            for source, tokens in registry.boards.items():
                for token in tokens:
                    with self.subTest(profile=name, source=source, board=token):
                        display = registry.names.get(token)
                        self.assertIsNotNone(
                            display,
                            f"{name}: board {source}:{token} has no sources.names entry, so "
                            "dedup.employer_display cannot resolve its employer",
                        )
                        assert display is not None
                        self.assertNotEqual(display.strip(), "")
                        # A display name is what a person reads. The board token
                        # is a slug and is on the dashboard's forbidden list.
                        self.assertNotEqual(display.strip(), token)

    def test_no_display_name_without_a_board(self) -> None:
        """A name for a board nobody polls is a stale entry, and it still replays the corpus."""
        for name in PROFILE_NAMES:
            registry = load_profile(PROFILES / name).sources
            registered = {token for tokens in registry.boards.values() for token in tokens}
            for token in registry.names:
                with self.subTest(profile=name, board=token):
                    self.assertIn(token, registered)

    def test_every_registered_source_has_an_adapter(self) -> None:
        """A board under a source Discover cannot fetch would never be polled."""
        for name in PROFILE_NAMES:
            registry = load_profile(PROFILES / name).sources
            for source, tokens in registry.boards.items():
                if not tokens:
                    continue
                with self.subTest(profile=name, source=source):
                    self.assertIn(source, ADAPTERS)


if __name__ == "__main__":
    unittest.main()
