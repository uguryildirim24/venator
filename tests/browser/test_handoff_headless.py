"""VENATOR_HANDOFF_HEADLESS is honoured without launching a real browser."""

from __future__ import annotations

from pathlib import Path

import pytest

from venator.browser.handoff import _open_browser


class _Page:
    url = "about:blank"


class _Context:
    def __init__(self) -> None:
        self.pages = [_Page()]
        self.launches: list[dict[str, object]] = []

    def launch_persistent_context(self, **options: object) -> _Context:
        self.launches.append(dict(options))
        return self

    def set_default_timeout(self, _value: object) -> None:
        return None

    def new_page(self) -> _Page:
        return _Page()


class _Chromium:
    def __init__(self, context: _Context) -> None:
        self._context = context

    def launch_persistent_context(self, **options: object) -> _Context:
        return self._context.launch_persistent_context(**options)


class _Playwright:
    def __init__(self) -> None:
        self.context = _Context()
        self.chromium = _Chromium(self.context)

    def start(self) -> _Playwright:
        return self

    def stop(self) -> None:
        return None


def _request(tmp_path: Path) -> dict[str, str]:
    return {"browser_directory": str(tmp_path / "browser")}


def test_handoff_headless_option_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Playwright()
    monkeypatch.setattr("venator.browser.handoff.sync_playwright", lambda: fake)

    monkeypatch.setenv("VENATOR_HANDOFF_HEADLESS", "1")
    _open_browser(_request(tmp_path))
    assert fake.context.launches[0]["headless"] is True

    monkeypatch.delenv("VENATOR_HANDOFF_HEADLESS", raising=False)
    fake.context.launches.clear()
    _open_browser(_request(tmp_path))
    assert fake.context.launches[0]["headless"] is False

    monkeypatch.setenv("VENATOR_HANDOFF_HEADLESS", "true")
    fake.context.launches.clear()
    _open_browser(_request(tmp_path))
    assert fake.context.launches[0]["headless"] is False

    # Every launch attempt carries it, not only the bundled one: the chrome and
    # msedge fallbacks are what a machine without bundled Chromium reaches.
    class _Refusing(_Context):
        def launch_persistent_context(self, **options: object) -> _Context:
            self.launches.append(dict(options))
            if options.get("channel") != "msedge":
                raise RuntimeError("no such browser")
            return self

    fallback = _Playwright()
    fallback.context = _Refusing()
    fallback.chromium = _Chromium(fallback.context)
    monkeypatch.setattr("venator.browser.handoff.sync_playwright", lambda: fallback)
    for value, expected in (("1", True), (None, False)):
        if value is None:
            monkeypatch.delenv("VENATOR_HANDOFF_HEADLESS", raising=False)
        else:
            monkeypatch.setenv("VENATOR_HANDOFF_HEADLESS", value)
        fallback.context.launches.clear()
        _open_browser(_request(tmp_path))
        assert [launch.get("channel") for launch in fallback.context.launches] == [None, "chrome", "msedge"]
        assert all(launch["headless"] is expected for launch in fallback.context.launches)
