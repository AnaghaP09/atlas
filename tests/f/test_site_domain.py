"""The site domain is a setting: `internal` for an organisation, `localhost` for one computer."""

from pathlib import Path

from atlas.config import load_settings
from atlas.site import SiteConfig
from deploy.init import host_names
from deploy.steps import one_time_steps


def test_default_domain_is_internal():
    site = SiteConfig(org_slug="demo")
    assert site.host_name == "atlas.demo.internal"
    assert host_names("demo") == ["atlas.demo.internal", "scenario.demo.internal"]


def test_localhost_domain_builds_localhost_names():
    site = SiteConfig(org_slug="demo", domain="localhost")
    assert site.host_name == "atlas.demo.localhost"
    assert site.origin == "https://atlas.demo.localhost"
    assert SiteConfig(org_slug="demo", domain="localhost", scenario=True).host_name == "scenario.demo.localhost"
    assert host_names("demo", "localhost") == ["atlas.demo.localhost", "scenario.demo.localhost"]


def test_localhost_names_need_no_hosts_entry():
    text = one_time_steps(host_names("demo", "localhost"), Path("ca.crt"), system="windows")
    assert "No hosts entry needed" in text
    assert "drivers\\etc\\hosts" not in text
    assert "certutil -user -addstore Root" in text


def test_internal_names_keep_the_hosts_step():
    text = one_time_steps(host_names("demo"), Path("ca.crt"), system="windows")
    assert "drivers\\etc\\hosts" in text


def test_setting_is_validated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ATLAS_SITE_DOMAIN=localhost\n", encoding="utf-8")
    assert load_settings(env).site_domain == "localhost"
