"""Stable pseudonyms for people: names, handles and emails never reach a committed file.

The rules live in ``atlas.cleaning`` so the offline preparation and the "Data as of" copies built in
the server clean people the same way. Here the salt and the handle inventory live in ``data/local``
(never committed); the handle list is also written to ``data/local/handles.txt`` so the
repository's public check can catch any handle that slips into a committed file.
"""

from __future__ import annotations

from pathlib import Path

from atlas.cleaning import (DATA_URI, EMAIL, ENCODED_DATA_URI, GENERIC_BOTS, JWT, MENTION, NOT_HANDLES,  # noqa: F401
                            SIGNED_PARAM, SSH_KEY)
from atlas.cleaning import People as _People
from prep.reference import paths, reference

LOCAL = paths()["local"]
NON_PERSON = GENERIC_BOTS | {b.lower() for b in reference().project.bot_accounts}


class People(_People):
    def __init__(self, local_dir: Path = LOCAL) -> None:
        super().__init__(local_dir, reference().project.bot_accounts)
