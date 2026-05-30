"""CLI diagnostic: print arc data for every issue in a volume.

Usage::

    python -m app.scripts.inspect_volume_arcs <volume_cv_id>

For each issue in the volume, prints:
    [H/S] cv_id  issue_number  name                 arcs: <ids+names or "(none)">

Where H = hydrated (raw_payload + fetched_at populated) and S = stub.

If every hydrated issue prints ``arcs: (none)``, then CV genuinely doesn't
have any ``story_arc_credits`` for this volume's issues — and that's why
the volume page won't show arc stripes. (CV only sets story_arc_credits
when an issue is explicitly tagged as part of a tracked arc; many volumes
have no arcs at all.)

If hydrated issues have arc IDs, then the arc-fill code in
``get_volume_detail`` should be discovering them and we have a real bug
to chase.
"""

import asyncio
import sys

from sqlalchemy import select

from app.comicvine import ComicVineCache, ComicVineClient
from app.db import SessionLocal
from app.models import CvIssue, CvVolume
from app.services.cv_helpers import sort_key_issue_number
from app.services.library import get_volume_detail


async def _main(vol_cv_id: int) -> int:
    async with SessionLocal() as db:
        vol = await db.get(CvVolume, vol_cv_id)
        if vol is None:
            print(f"error: volume {vol_cv_id} not in cache", file=sys.stderr)
            return 1
        print(f"Volume: {vol.name} (cv_id={vol.cv_id}, year={vol.year})")
        print(f"count_of_issues={vol.count_of_issues}")
        print()

        issues = list(
            (
                await db.execute(
                    select(CvIssue).where(CvIssue.volume_cv_id == vol_cv_id)
                )
            ).scalars()
        )
        issues.sort(key=lambda i: sort_key_issue_number(i.issue_number))

        hydrated = 0
        with_arcs = 0
        all_arc_ids: set[int] = set()

        for issue in issues:
            is_hydrated = issue.fetched_at is not None
            flag = "H" if is_hydrated else "S"
            payload = issue.raw_payload if isinstance(issue.raw_payload, dict) else {}
            arc_credits = payload.get("story_arc_credits") or []
            if is_hydrated:
                hydrated += 1
            arcs_repr: str
            if arc_credits:
                with_arcs += 1
                pairs = []
                for arc in arc_credits:
                    aid = arc.get("id")
                    aname = arc.get("name") or "?"
                    if aid is not None:
                        all_arc_ids.add(int(aid))
                        pairs.append(f"{aid}:{aname}")
                arcs_repr = ", ".join(pairs)
            else:
                arcs_repr = "(none)"
            name = (issue.name or "")[:40]
            print(
                f"  [{flag}] {issue.cv_id:>8}  #{issue.issue_number or '?':<6}  "
                f"{name:<42}  arcs: {arcs_repr}"
            )

        print()
        print(
            f"Summary: {len(issues)} issues total, {hydrated} hydrated, "
            f"{with_arcs} with arc credits, {len(all_arc_ids)} unique arc IDs"
        )
        if all_arc_ids:
            print(f"Unique arc IDs across volume: {sorted(all_arc_ids)}")

        # End-to-end check: actually call get_volume_detail the way the
        # route does, and report what arc_slots / arc_color_classes it
        # produces. If this is empty when the per-issue inspection above
        # found arc credits, the bug is somewhere in the service layer.
        print()
        print("=== get_volume_detail() result ===")
        client = ComicVineClient()
        try:
            cache = ComicVineCache(client)
            detail = await get_volume_detail(db, vol_cv_id, cv_cache=cache)
        finally:
            await client.aclose()
        if detail is None:
            print("get_volume_detail returned None")
            return 1
        print(f"arc_slots: {[(a.cv_id, a.name) for a in detail.arc_slots]}")
        print(f"arc_color_classes: {detail.arc_color_classes}")
        print(f"story_arc_names: {detail.story_arc_names}")
        # Per-issue arc_credits as seen by the template:
        with_template_arcs = sum(1 for i in detail.issues if i.arc_credits)
        print(
            f"Issues with arc_credits in VolumeDetail: {with_template_arcs} "
            f"of {len(detail.issues)}"
        )
        for irow in detail.issues[:10]:
            ids = [a.cv_id for a in irow.arc_credits]
            print(f"  #{irow.issue_number}: arc_credits cv_ids = {ids}")
        return 0


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "usage: python -m app.scripts.inspect_volume_arcs <volume_cv_id>",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        vol_cv_id = int(sys.argv[1])
    except ValueError:
        print("error: volume_cv_id must be an integer", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main(vol_cv_id)))


if __name__ == "__main__":
    main()
