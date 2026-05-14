from __future__ import annotations

from anonymize import env_check


def test_run_returns_report() -> None:
    rep = env_check.run()
    assert isinstance(rep, env_check.EnvReport)
    assert rep.tools, "should at least probe llama-server / pandoc / etc."
    assert isinstance(rep.summary(), str)


def test_check_tool_for_missing_binary() -> None:
    s = env_check.check_tool("definitely-not-installed-xyzz", required=False, description="fake")
    assert s.found is False
