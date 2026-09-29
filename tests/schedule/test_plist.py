from __future__ import annotations

import plistlib
from pathlib import Path

from venator.schedule.agent import LABEL, launch_agent, render


def test_installed_agent_daily_time_and_install(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    install = tmp_path / 'Install'
    interpreter = tmp_path / 'Venator.app/Contents/Resources/resources/python/aarch64-apple-darwin/python/bin/python3'
    plist = plistlib.loads(render(launch_agent(home=home, install=install, interpreter=interpreter, claude=home / '.local/bin/claude',
                                               profile='someone', hour=7, minute=15,
                                               daily_usd=1.0, monthly_usd=10.0)))
    assert plist['Label'] == LABEL
    assert plist['ProgramArguments'] == [str(interpreter), '-m', 'venator.schedule.loop', '--profile', 'someone']
    assert plist['StartCalendarInterval'] == {'Hour': 7, 'Minute': 15}
    assert 'WorkingDirectory' not in plist
    assert plist['EnvironmentVariables']['VENATOR_HOME'] == str(install)
    assert plist['EnvironmentVariables']['VENATOR_SCORE_DAILY_USD'] == '1.0'
    assert plist['EnvironmentVariables']['VENATOR_SCORE_MONTHLY_USD'] == '10.0'
    assert str(home / '.local/bin') in plist['EnvironmentVariables']['PATH']
    assert 'RunAtLoad' not in plist


def test_default_start_time(tmp_path: Path) -> None:
    assert launch_agent(home=tmp_path)['StartCalendarInterval'] == {'Hour': 6, 'Minute': 30}
