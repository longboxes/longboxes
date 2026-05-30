"""CLI helper to reset an existing user's password from the host.

The deliberate twin of ``create_user.py`` — that script refuses to
overwrite an existing row to avoid silent role / state surprises;
this one is purpose-built for the "I forgot the password" case and
requires the username to already exist.

Usage::

    python -m app.scripts.reset_password <username> <new_password>

Exits 1 on validation errors (missing user, password too short,
etc.). Does not touch the user's role or any other fields — only
``password_hash`` is rewritten.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models import User


async def _main(username: str, password: str) -> int:
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 1

    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None:
            # Distinct from create_user's "already exists" path —
            # here a missing user is the actual failure mode.
            print(
                f"error: user {username!r} not found (use create_user to make a new one)",
                file=sys.stderr,
            )
            return 1

        user.password_hash = hash_password(password)
        await session.commit()

    print(f"password reset for {user.role} user: {username}")
    return 0


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "usage: python -m app.scripts.reset_password <username> <new_password>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_main(sys.argv[1], sys.argv[2])))


if __name__ == "__main__":
    main()
