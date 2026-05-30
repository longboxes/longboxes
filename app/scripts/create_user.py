"""CLI helper to create a user without going through the web UI.

Useful for:
- Recovery (admin forgot their password — create a fresh admin from the host).
- Provisioning a viewer non-interactively in scripts.

Usage::

    python -m app.scripts.create_user <username> <password> [admin|viewer]

Exits 1 on validation errors.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models import User, UserRole


async def _main(username: str, password: str, role: str) -> int:
    if role not in {UserRole.ADMIN, UserRole.VIEWER}:
        print(f"error: role must be 'admin' or 'viewer', got {role!r}", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 1

    async with SessionLocal() as session:
        existing = await session.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none() is not None:
            print(f"error: user {username!r} already exists", file=sys.stderr)
            return 1

        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
            )
        )
        await session.commit()

    print(f"created {role} user: {username}")
    return 0


def main() -> None:
    if len(sys.argv) not in (3, 4):
        print(
            "usage: python -m app.scripts.create_user <username> <password> [admin|viewer]",
            file=sys.stderr,
        )
        sys.exit(2)
    username = sys.argv[1]
    password = sys.argv[2]
    role = sys.argv[3] if len(sys.argv) == 4 else UserRole.VIEWER
    sys.exit(asyncio.run(_main(username, password, role)))


if __name__ == "__main__":
    main()
