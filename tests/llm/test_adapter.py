"""Selection: one runtime runs, and it is never chosen for the person.

The property under test throughout this file is a negative one — that a lane
which could have answered was *not asked*. Every test that matters here asserts
`asked == 0` on somebody.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from venator.llm import complete
from venator.llm.runtime import (
    Answer,
    Invocation,
    LaneFailed,
    LaneUnavailable,
    NoRuntimeAvailable,
    Request,
    Result,
    Runner,
)


class StubLane:
    """A lane that answers, declines, or breaks — and remembers being asked."""

    def __init__(
        self,
        name: str,
        *,
        answer: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.answer = answer
        self.raises = raises
        self.asked = 0

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer:
        self.asked += 1
        if self.raises is not None:
            raise self.raises
        assert self.answer is not None
        return Answer(text=self.answer)


def declining(name: str) -> StubLane:
    return StubLane(
        name,
        raises=LaneUnavailable(
            name,
            f"the {name} command could not be run",
            f"install {name} and sign in to it yourself",
        ),
    )


def unusable(invocation: Invocation) -> Result:
    raise AssertionError("no lane in this test starts a child process")


def test_claude_is_the_default_and_nobody_else_is_asked() -> None:
    claude = StubLane("claude", answer="from the subscription")
    codex = StubLane("codex", answer="from codex")
    key = StubLane("api", answer="from a key")

    answer = complete("prompt", lanes=[claude, codex, key], runner=unusable, environ={})

    assert (answer.lane, answer.text) == ("claude", "from the subscription")
    assert (codex.asked, key.asked) == (0, 0)


def test_the_default_is_chosen_by_name_not_by_position() -> None:
    # If selection ever regresses to "the first lane in the list", this catches
    # it: claude is last here and must still be the one that answers.
    codex = StubLane("codex", answer="from codex")
    claude = StubLane("claude", answer="from the subscription")

    answer = complete("prompt", lanes=[codex, claude], runner=unusable, environ={})

    assert answer.lane == "claude"
    assert codex.asked == 0


def test_an_unavailable_claude_never_reaches_codex_or_a_key() -> None:
    # The whole point of the change: a person's spend does not move to another
    # sign-in or another account because the default lane was missing.
    claude = declining("claude")
    codex = StubLane("codex", answer="from codex")
    key = StubLane("api", answer="from a key")

    with pytest.raises(NoRuntimeAvailable):
        complete("prompt", lanes=[claude, codex, key], runner=unusable, environ={})

    assert (codex.asked, key.asked) == (0, 0)


def test_an_unauthenticated_claude_never_reaches_codex_or_a_key() -> None:
    # Same property for the other kind of absence. This is the one the old
    # marker heuristic could get wrong: a failure whose text merely looked like
    # a login problem used to be able to switch accounts. Now it cannot.
    claude = StubLane(
        "claude",
        raises=LaneUnavailable(
            "claude",
            "claude is installed but reported no usable login (unauthorized)",
            "sign in to the unmodified binary yourself with /login",
        ),
    )
    codex = StubLane("codex", answer="from codex")
    key = StubLane("api", answer="from a key")

    with pytest.raises(NoRuntimeAvailable):
        complete("prompt", lanes=[claude, codex, key], runner=unusable, environ={})

    assert (codex.asked, key.asked) == (0, 0)


def test_a_configured_key_lane_is_still_not_reached_for() -> None:
    # Configuring the key lane is not the same as asking for it. Only
    # VENATOR_LLM_RUNTIME=api asks for it.
    claude = declining("claude")
    key = StubLane("api", answer="from a key")
    configured = {
        "VENATOR_LLM_API_URL": "https://endpoint.example/v1/chat/completions",
        "VENATOR_LLM_API_MODEL": "a-model",
        "VENATOR_LLM_API_KEY_VAR": "MY_OWN_KEY",
        "MY_OWN_KEY": "the-key-the-owner-chose",
    }

    with pytest.raises(NoRuntimeAvailable):
        complete("prompt", lanes=[claude, key], runner=unusable, environ=configured)

    assert key.asked == 0


def test_a_lane_that_ran_and_broke_does_not_reach_another_lane() -> None:
    claude = StubLane("claude", raises=LaneFailed("claude -p exited 2: boom"))
    codex = StubLane("codex", answer="from codex")

    with pytest.raises(LaneFailed, match="exited 2"):
        complete("prompt", lanes=[claude, codex], runner=unusable, environ={})

    assert codex.asked == 0


def test_the_runtime_variable_selects_a_lane() -> None:
    claude = StubLane("claude", answer="from the subscription")
    codex = StubLane("codex", answer="from codex")

    answer = complete(
        "prompt",
        lanes=[claude, codex],
        runner=unusable,
        environ={"VENATOR_LLM_RUNTIME": "codex"},
    )

    assert (answer.lane, answer.text) == ("codex", "from codex")
    assert claude.asked == 0


def test_the_runtime_variable_selects_the_key_lane_when_it_says_so() -> None:
    claude = StubLane("claude", answer="from the subscription")
    key = StubLane("api", answer="from a key")

    answer = complete(
        "prompt",
        lanes=[claude, key],
        runner=unusable,
        environ={"VENATOR_LLM_RUNTIME": "api"},
    )

    assert answer.lane == "api"
    assert claude.asked == 0


def test_a_named_lane_that_cannot_run_does_not_fall_back_to_the_default() -> None:
    claude = StubLane("claude", answer="from the subscription")
    codex = declining("codex")

    with pytest.raises(NoRuntimeAvailable) as error:
        complete(
            "prompt",
            lanes=[claude, codex],
            runner=unusable,
            environ={"VENATOR_LLM_RUNTIME": "codex"},
        )

    assert claude.asked == 0
    assert "VENATOR_LLM_RUNTIME=codex names the runtime to use" in str(error.value)


def test_surrounding_whitespace_in_the_variable_does_not_select_nothing() -> None:
    codex = StubLane("codex", answer="from codex")

    answer = complete(
        "prompt",
        lanes=[StubLane("claude", answer="x"), codex],
        runner=unusable,
        environ={"VENATOR_LLM_RUNTIME": "  codex  "},
    )

    assert answer.lane == "codex"


def test_an_empty_variable_leaves_the_default_in_place() -> None:
    claude = StubLane("claude", answer="from the subscription")

    answer = complete(
        "prompt",
        lanes=[claude, StubLane("codex", answer="y")],
        runner=unusable,
        environ={"VENATOR_LLM_RUNTIME": ""},
    )

    assert answer.lane == "claude"


def test_an_unknown_runtime_name_is_refused_by_name() -> None:
    lanes = [StubLane("claude", answer="x"), StubLane("codex", answer="y")]

    with pytest.raises(ValueError, match="names no runtime; available: claude, codex"):
        complete(
            "prompt",
            lanes=lanes,
            runner=unusable,
            environ={"VENATOR_LLM_RUNTIME": "something-else"},
        )


def test_the_failure_names_the_runtime_the_remedy_and_the_silence() -> None:
    claude = declining("claude")

    with pytest.raises(NoRuntimeAvailable) as error:
        complete(
            "prompt",
            lanes=[claude, StubLane("codex", answer="y"), StubLane("api", answer="z")],
            runner=unusable,
            environ={},
        )

    message = str(error.value)
    assert message.startswith("The default LLM runtime is claude, and it cannot run.")
    assert "  claude: the claude command could not be run" in message
    assert "fix: install claude and sign in to it yourself" in message
    assert "Nothing else was tried." in message
    assert "VENATOR_LLM_RUNTIME=codex|api" in message


def test_the_failure_does_not_advertise_the_lane_that_just_failed() -> None:
    with pytest.raises(NoRuntimeAvailable) as error:
        complete(
            "prompt",
            lanes=[declining("claude"), StubLane("codex", answer="y")],
            runner=unusable,
            environ={},
        )

    assert "VENATOR_LLM_RUNTIME=codex." in str(error.value)
    assert "claude|" not in str(error.value)


def test_the_prompt_model_and_timeout_reach_the_lane() -> None:
    seen: list[Request] = []

    class Capturing:
        name = "claude"

        def complete(
            self,
            request: Request,
            runner: Runner,
            environ: Mapping[str, str],
        ) -> Answer:
            seen.append(request)
            return Answer(text="ok")

    complete(
        "the prompt",
        model="sonnet",
        timeout=7.0,
        lanes=[Capturing()],
        runner=unusable,
        environ={},
    )

    assert seen == [Request(prompt="the prompt", model="sonnet", timeout=7.0)]
