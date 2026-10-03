import pytest

from btc15.config import ConfigError, validate_for_start
from conftest import make_cfg

LIVE_OK = dict(MODE="LIVE", KALSHI_KEY_ID="k", KALSHI_PRIVATE_KEY_PATH="/x.pem", DASHBOARD_PASSWORD="longenough1",
               LIVE_CONFIRM="yes", DAILY_LOSS_LIMIT_USD=20.0, MAX_TOTAL_LOSS_USD=100.0, MAX_CONTRACTS_PER_TRADE=3,
               ALLOW_UNPROVEN="yes")


def test_live_starts_when_everything_is_set():
    validate_for_start(make_cfg(**LIVE_OK), "UNPROVEN")
    validate_for_start(make_cfg(**{**LIVE_OK, "ALLOW_UNPROVEN": None}), "PROVEN")


@pytest.mark.parametrize("missing", ["LIVE_CONFIRM", "DAILY_LOSS_LIMIT_USD", "MAX_TOTAL_LOSS_USD",
                                     "MAX_CONTRACTS_PER_TRADE", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "DASHBOARD_PASSWORD"])
def test_live_refuses_to_start_without_required_setting(missing):
    kw = {**LIVE_OK, missing: None}
    with pytest.raises(ConfigError, match=missing):
        validate_for_start(make_cfg(**kw), "UNPROVEN")


@pytest.mark.parametrize("bad", [dict(LIVE_CONFIRM="y"), dict(LIVE_CONFIRM="true"), dict(DAILY_LOSS_LIMIT_USD=0.0),
                                 dict(MAX_TOTAL_LOSS_USD=-5.0), dict(MAX_CONTRACTS_PER_TRADE=0)])
def test_live_refuses_invalid_values(bad):
    with pytest.raises(ConfigError):
        validate_for_start(make_cfg(**{**LIVE_OK, **bad}), "PROVEN")


def test_live_unproven_needs_allow_unproven():
    with pytest.raises(ConfigError, match="ALLOW_UNPROVEN"):
        validate_for_start(make_cfg(**{**LIVE_OK, "ALLOW_UNPROVEN": None}), "UNPROVEN")
    with pytest.raises(ConfigError, match="ALLOW_UNPROVEN"):
        validate_for_start(make_cfg(**{**LIVE_OK, "ALLOW_UNPROVEN": "true"}), "UNPROVEN")


def test_unproven_live_forces_one_contract_regardless_of_config():
    cfg = make_cfg(**LIVE_OK)
    assert cfg.effective_max_contracts("UNPROVEN") == 1
    assert cfg.effective_max_contracts("PROVEN") == 3


def test_paper_needs_only_a_dashboard_password():
    validate_for_start(make_cfg(DASHBOARD_PASSWORD="longenough1"), "UNPROVEN")
    with pytest.raises(ConfigError):
        validate_for_start(make_cfg(), "UNPROVEN")
    with pytest.raises(ConfigError):
        validate_for_start(make_cfg(DASHBOARD_PASSWORD="short"), "UNPROVEN")


def test_demo_needs_credentials():
    with pytest.raises(ConfigError, match="KALSHI_KEY_ID"):
        validate_for_start(make_cfg(MODE="DEMO", DASHBOARD_PASSWORD="longenough1"), "UNPROVEN")


def test_default_mode_is_paper_and_environments_are_isolated():
    assert make_cfg().MODE == "PAPER"
    assert "demo" in make_cfg(MODE="DEMO").rest_base
    assert "demo" not in make_cfg(MODE="LIVE").rest_base and "demo" not in make_cfg().rest_base


def test_params_version_tracks_strategy_not_mode_or_secrets():
    a = make_cfg()
    assert a.params_version() == make_cfg(MODE="DEMO", KALSHI_KEY_ID="x").params_version()
    assert a.params_version() != make_cfg(MIN_EDGE=0.06).params_version()
    assert a.params_version() != make_cfg(VOL_SOURCE="realized").params_version()
    assert len(a.params_version()) == 12
