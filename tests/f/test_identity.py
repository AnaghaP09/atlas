from __future__ import annotations

from types import SimpleNamespace

import pytest

from atlas.contracts import IdentityMode, IdentityProvider, Role
from atlas.identity import (GENERIC_FAILURE, FailureLimiter, LocalIdentity, SessionResolver, SignInRefused,
                            UserDirectory, UserDirectoryError, hash_password, verify_password, write_users_file)
from atlas.sessions import COOKIE_NAME, SessionStore

FAST = {"n": 2**10, "r": 8, "p": 1}


class Clock:
    def __init__(self) -> None:
        self.now = 5_000_000.0

    def __call__(self) -> float:
        return self.now


def make_dir(tmp_path, entries):
    path = tmp_path / "users.toml"
    write_users_file(path, entries)
    return UserDirectory.load(path)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def provider(tmp_path, clock):
    users = make_dir(tmp_path, [
        {"name": "pm", "display_name": "Product manager (demo)", "password_hash": hash_password("right", **FAST),
         "roles": {"kubevirt": "pm", "argo-cd": "pm"}},
        {"name": "lead", "display_name": "Lead (demo)", "password_hash": hash_password("lead-pw", **FAST),
         "roles": {"kubevirt": "em", "argo-cd": "pm"}},
        {"name": "nobody", "display_name": "No projects", "password_hash": hash_password("x", **FAST), "roles": {}},
    ])
    sessions = SessionStore(tmp_path / "s.sqlite", clock=clock)
    return LocalIdentity(users, sessions, default_project="kubevirt", limiter=FailureLimiter(clock=clock))


def request(cookies=None, headers=None, project=None, path_project=None):
    return SimpleNamespace(cookies=cookies or {}, headers=headers or {},
                           query_params={"project_id": project} if project else {},
                           path_params={"project_id": path_project} if path_project else {})


def test_password_hash_round_trip_and_format():
    stored = hash_password("s3cret", **FAST)
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert verify_password("s3cret", stored)
    assert not verify_password("other", stored)
    assert not verify_password("s3cret", "garbage") and not verify_password("s3cret", "md5$x$y$z$a$b")


def test_provider_implements_shared_interface(provider):
    assert isinstance(provider, IdentityProvider)
    assert provider.mode is IdentityMode.LOCAL


def test_sign_in_and_role_per_project(provider):
    result = provider.sign_in("lead", "lead-pw")
    assert result.ok
    cookies = {COOKIE_NAME: result.token}
    assert provider.authenticate(request(cookies)).role is Role.EM  # default project kubevirt
    assert provider.authenticate(request(cookies, project="argo-cd")).role is Role.PM
    assert provider.authenticate(request(cookies, path_project="argo-cd")).role is Role.PM


def test_non_member_project_is_refused(provider):
    token = provider.sign_in("pm", "right").token
    with pytest.raises(SignInRefused) as exc:
        provider.authenticate(request({COOKIE_NAME: token}, project="someone-else"))
    assert exc.value.status == 403


def test_wrong_password_and_unknown_user_get_the_same_message(provider):
    assert provider.sign_in("pm", "wrong").message == GENERIC_FAILURE
    assert provider.sign_in("ghost", "anything").message == GENERIC_FAILURE


def test_user_without_projects_cannot_sign_in(provider):
    assert not provider.sign_in("nobody", "x").ok


def test_rate_limit_blocks_after_five_failures_then_expires(provider, clock):
    for _ in range(5):
        assert not provider.sign_in("pm", "wrong").ok
    assert not provider.sign_in("pm", "right").ok  # blocked even with the right password
    clock.now += 15 * 60 + 1
    assert provider.sign_in("pm", "right").ok


def test_successful_sign_in_resets_failures(provider):
    for _ in range(4):
        provider.sign_in("pm", "wrong")
    assert provider.sign_in("pm", "right").ok
    for _ in range(4):
        provider.sign_in("pm", "wrong")
    assert provider.sign_in("pm", "right").ok


def test_sign_in_rotates_any_previous_session(provider):
    first = provider.sign_in("pm", "right").token
    second = provider.sign_in("pm", "right", previous_token=first).token
    assert provider.sessions.validate(first) is None
    assert provider.sessions.validate(second) is not None


def test_role_header_is_refused_in_sign_in_mode(provider):
    token = provider.sign_in("pm", "right").token
    with pytest.raises(SignInRefused) as exc:
        provider.authenticate(request({COOKIE_NAME: token}, headers={"x-atlas-role": "audit"}))
    assert exc.value.status == 401


def test_no_cookie_means_not_signed_in(provider):
    assert provider.authenticate(request()) is None


def test_resolver_maps_refusals_to_api_errors(provider):
    from atlas.api.identity import IdentityError

    resolver = SessionResolver(provider)
    with pytest.raises(IdentityError) as exc:
        resolver.resolve(request(headers={"x-atlas-role": "pm"}))
    assert exc.value.status == 401
    token = provider.sign_in("pm", "right").token
    assert resolver.resolve(request({COOKIE_NAME: token})).role is Role.PM


def test_user_file_rejects_unknown_roles_and_missing_hashes(tmp_path):
    bad = tmp_path / "users.toml"
    bad.write_text('[users.x]\npassword_hash = "scrypt$1$1$1$a$b"\n[users.x.roles]\nkubevirt = "boss"\n')
    with pytest.raises(UserDirectoryError):
        UserDirectory.load(bad)
    bad.write_text('[users.x]\ndisplay_name = "X"\n')
    with pytest.raises(UserDirectoryError):
        UserDirectory.load(bad)
    with pytest.raises(UserDirectoryError):
        UserDirectory.load(tmp_path / "missing.toml")


def test_user_file_never_contains_passwords(tmp_path):
    path = tmp_path / "users.toml"
    write_users_file(path, [{"name": "pm", "display_name": "PM", "password_hash": hash_password("plain-pw", **FAST),
                             "roles": {"kubevirt": "pm"}}])
    assert "plain-pw" not in path.read_text()
