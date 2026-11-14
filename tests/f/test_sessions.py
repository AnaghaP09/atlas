from __future__ import annotations

import pytest

from atlas.sessions import (ABSOLUTE_SECONDS, COOKIE_NAME, IDLE_SECONDS, SessionStore, clear_cookie,
                            session_cookie)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> SessionStore:
    return SessionStore(tmp_path / "sessions.sqlite", clock=clock)


def test_session_round_trip_stores_only_a_hash(store, tmp_path):
    token, session = store.create("pm")
    assert store.validate(token).user == "pm"
    assert token not in (tmp_path / "sessions.sqlite").read_bytes().decode("latin-1")
    assert session.session_id != token


def test_idle_limit_ends_session(store, clock):
    token, _ = store.create("pm")
    clock.now += IDLE_SECONDS - 1
    assert store.validate(token) is not None  # a user request extends idle time
    clock.now += IDLE_SECONDS - 1
    assert store.validate(token) is not None
    clock.now += IDLE_SECONDS
    assert store.validate(token) is None


def test_absolute_limit_ends_session_even_when_active(store, clock):
    token, _ = store.create("pm")
    for _ in range(ABSOLUTE_SECONDS // (IDLE_SECONDS // 2) + 2):
        clock.now += IDLE_SECONDS // 2
        store.validate(token)
    assert store.validate(token) is None


def test_lookup_and_untouched_validate_do_not_extend_idle_time(store, clock):
    token, session = store.create("pm")
    clock.now += IDLE_SECONDS - 5
    assert store.validate(token, touch=False) is not None
    assert store.lookup(session.session_id) is not None
    clock.now += 10
    assert store.validate(token, touch=False) is None


def test_rotation_replaces_token_and_csrf(store):
    token, session = store.create("pm")
    new_token, new_session = store.rotate(token)
    assert new_token != token and new_session.csrf_token != session.csrf_token
    assert store.validate(token) is None
    assert store.validate(new_token).user == "pm"


def test_sign_out_closes_open_streams(store):
    token, session = store.create("pm")
    handle = store.bind_stream(session)
    closed = []
    handle.on_close(lambda: closed.append(True))
    assert handle.still_valid() and store.open_streams(session.session_id) == 1
    store.revoke(token)
    assert handle.closed and closed == [True]
    assert not handle.still_valid()
    assert store.open_streams(session.session_id) == 0


@pytest.mark.parametrize("advance", [IDLE_SECONDS, ABSOLUTE_SECONDS])
def test_expiry_closes_stream_on_next_check(store, clock, advance):
    _, session = store.create("pm")
    handle = store.bind_stream(session)
    clock.now += advance
    assert handle.still_valid() is False
    assert handle.closed


def test_stream_traffic_does_not_keep_session_alive(store, clock):
    token, session = store.create("pm")
    handle = store.bind_stream(session)
    for _ in range(IDLE_SECONDS // 15 + 2):
        clock.now += 15
        handle.still_valid()
    assert store.validate(token, touch=False) is None
    assert handle.closed


def test_sweep_ends_expired_sessions_and_their_streams(store, clock):
    _, session = store.create("pm")
    handle = store.bind_stream(session)
    clock.now += IDLE_SECONDS + 1
    assert store.sweep() == 1
    assert handle.closed


def test_revoke_user_ends_every_session(store):
    a, _ = store.create("pm")
    b, _ = store.create("pm")
    c, _ = store.create("em")
    store.revoke_user("pm")
    assert store.validate(a) is None and store.validate(b) is None and store.validate(c) is not None


def test_csrf_check(store):
    _, session = store.create("pm")
    assert session.csrf_ok(session.csrf_token)
    assert not session.csrf_ok(None) and not session.csrf_ok("") and not session.csrf_ok("x" * 43)


def test_cookie_attributes():
    value = session_cookie("tok")
    assert value.startswith(f"{COOKIE_NAME}=tok;")
    for part in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
        assert part in value
    assert "Domain" not in value
    assert "Max-Age=0" in clear_cookie()
