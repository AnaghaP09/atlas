"""Container image digests are not handles, and redaction never leaves residue its own check flags.

A CI log line such as ``quay.io/org/<random tag>@sha256:<digest>`` used to become
``.../[REDACTED:high_entropy]@sha256:...`` after redaction; the residual scan then read "@sha256"
as a handle, withheld the payload and tripped the personal-data kill switch.
"""

from __future__ import annotations

from atlas.outbound import Redactor

R = Redactor()
# A random-looking image tag, assembled at run time so the repository holds no secret-like literal.
TAG = "".join(["Xk9Qv2", "Lm7Tz4", "Rw8Pq3", "Ns6"])


def test_real_handles_are_still_redacted() -> None:
    r = R.redact("cc @alice and @bob-2 for review")
    assert "@alice" not in r.text and "@bob-2" not in r.text and r.counts["handle"] == 2
    assert R.scan(r.text) == []


def test_image_digest_after_a_redacted_tag_leaves_no_residue() -> None:
    line = f"pulling quay.io/kubevirt/{TAG}@sha256:6d5e8e306aa5f0c7b1e2d3c4b5a69788"
    r = R.redact(line)
    assert "@sha256:6d5e8e306aa5f0c7b1e2d3c4b5a69788" in r.text
    assert R.scan(r.text) == []


def test_a_handle_exposed_by_an_earlier_replacement_is_redacted() -> None:
    # Whatever an earlier step replaces, the final text never contains an unredacted handle.
    for text in [f"{TAG}@reviewer", "10.0.0.1@someone", "host.internal.example@person"]:
        r = R.redact(text)
        assert R.scan(r.text) == [], (text, r.text)
