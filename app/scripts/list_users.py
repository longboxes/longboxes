"""CLI helper to enumerate all users from the host.

Companion to ``reset_password.py``: if you forgot the password AND
the username, this tells you what accounts exist so you can pick
one to reset. Prints username + role + created-at; the
password_hash column is intentionally never echoed.

Usage::

    python -m app.scripts.list_users
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User


async def _main() -> int:
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(User).order_by(User.role.desc(), User.username)))
            .scalars()
            .all()
        )

    if not rows:
        print("(no users)")
        return 0

    # Two-column width: longest username and "ROLE"/"admin"/"viewer".
    # Right-padded so the role column lines up cleanly regardless of
    # username length.
    name_width = max(8, max(len(u.username) for u in rows))
    print(f"{'USERNAME'.ljust(name_width)}  ROLE     CREATED")
    print(f"{'-' * name_width}  ------   -------")
    for u in rows:
        created = u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else ""
        print(f"{u.username.ljust(name_width)}  {u.role.ljust(7)}  {created}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
