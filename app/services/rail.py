"""Arc-flow rail visualization model.

Builds a window-scoped rail describing arc branches, nodes, and edge
treatments (diverge bend, merge bend, external arrows, vertical fade).
The view layer (Jinja macro ``_arc_rail.html``) consumes this model and
emits SVG directly — no client-side rendering.

Two builders, one model: ``build_list_rail`` indexes nodes per issue
(List view), ``build_gallery_rail`` indexes nodes per shelf (Gallery
view). Both produce the same ``RailModel`` shape so the SVG template
doesn't care which view it's rendering for.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.services.library import ArcCredit, VolumeIssueRow

if TYPE_CHECKING:
    from app.services.library import GallerySegment

# Vivid 400-tier hex palette — kept parallel to ``_ARC_BG_PALETTE`` in
# library.py so an arc renders the same color across the stripe column,
# legend, gallery container, and now the rail. Index by arc display
# position (the publication-order index, ``arc_display_order``).
_ARC_HEX_PALETTE: tuple[str, ...] = (
    "#fbbf24",  # amber-400
    "#34d399",  # emerald-400
    "#38bdf8",  # sky-400
    "#a78bfa",  # violet-400
    "#fb7185",  # rose-400
    "#22d3ee",  # cyan-400
    "#a3e635",  # lime-400
    "#e879f9",  # fuchsia-400
)


def arc_hex_for_index(idx: int) -> str:
    """Pick the rail color for the arc at display index ``idx``.

    Cycles through ``_ARC_HEX_PALETTE`` for volumes with more arcs than
    palette entries (visual ambiguity is acceptable past 8 arcs)."""
    return _ARC_HEX_PALETTE[idx % len(_ARC_HEX_PALETTE)]


@dataclass
class RailNode:
    """One node on a branch — a single issue's intersection with an arc.

    ``gap_after`` flags rows where the arc visits another volume between
    this node and the next in-window node. The template renders a
    rightward bow-out detour for that segment instead of a straight
    vertical line, making the "the arc went elsewhere and came back"
    journey visible without splitting the branch.
    """

    arc_cv_id: int
    row_index: int  # 0-based within the rail's window
    cv_id: int
    gap_after: bool = False


@dataclass
class RailBranch:
    """One arc's full in-window presence — a single connected branch
    even if the arc dips out of this volume and returns.

    Bow-out detours at each in-window node where the arc visited another
    volume before the next in-window node (see ``RailNode.gap_after``).
    Top and bottom edges still get the appropriate treatment for the
    arc's overall window-spanning behavior.

    ``is_external`` partitions branches across the spine: external arcs
    (anything connected to another volume — at top, bottom, or via a
    gap detour) sit to the *left* of the spine with leftward bends and
    arrows; internal arcs sit to the *right*. The split keeps the two
    populations visually distinct.
    """

    arc: ArcCredit
    color: str  # resolved hex; pulled from the arc-display-index palette
    nodes: list[RailNode]
    lane: int = 0

    # Top edge: how the branch enters the visible window. Exactly one
    # of these flags is True for any non-empty branch.
    top_diverge: bool = False  # bend from spine — arc starts here
    top_enters_from_prev_vol: bool = False  # arrow — prev member elsewhere
    top_fades: bool = False  # vertical line continues above window

    # Bottom edge: mirror of top.
    bottom_merge: bool = False  # bend to spine — arc ends here
    bottom_exits_to_next_vol: bool = False  # arrow — next member elsewhere
    bottom_fades: bool = False  # vertical line continues below window

    @property
    def has_gaps(self) -> bool:
        """True when any in-window node has a gap-detour after it."""
        return any(node.gap_after for node in self.nodes)

    @property
    def is_external(self) -> bool:
        """True when this arc touches another volume in any way."""
        return self.top_enters_from_prev_vol or self.bottom_exits_to_next_vol or self.has_gaps


@dataclass
class RailModel:
    """Geometry-ready rail description for one view + one window.

    Coordinate system: y=0 at the top, y increases downward.

    Two coexisting positioning models:
      * Uniform-pitch (List view): all rows share ``row_height`` and
        node y-center for row ``r`` is ``(r + 0.5) * row_height``.
      * Variable-pitch (Gallery view, where multi-arc shelves have
        more padding than single-arc): explicit per-row y-centers in
        ``row_y_centers``. The template prefers ``row_y_centers``
        when populated, falling back to the uniform formula otherwise.
    """

    width: int  # px — total SVG width
    height: int  # px — total SVG height
    row_height: int  # px — uniform-pitch spacing (List view); falls
    # through to row_y_centers when set (Gallery)
    spine_x: int  # px — x-coord of the spine line
    lane_spacing: int  # px — horizontal spacing between lanes
    bend_offset: int  # px — vertical span of diverge/merge bends
    branches: list[RailBranch] = field(default_factory=list)
    # Rows where 2+ branches share a node — get a faint horizontal
    # connector. Each entry is (row_index, sorted node x-coords).
    # X coords are pre-resolved (side-aware) so the template doesn't
    # need to know about left vs right lanes here.
    multi_arc_rows: list[tuple[int, list[int]]] = field(default_factory=list)
    # Variable-pitch row y-centers, one per rail row. When populated,
    # supersedes uniform ``row_height`` for node y-coord computation.
    row_y_centers: list[int] = field(default_factory=list)


def build_list_rail(
    window_issues: list[VolumeIssueRow],
    arc_members_by_aid: dict[int, list[dict]],
    arc_credit_for_aid: dict[int, ArcCredit],
    arc_display_order: list[int],
    in_volume_issue_ids: set[int],
    row_height: int = 72,
    lane_spacing: int = 14,
    bend_offset: int = 16,
    spine_x: int = 14,
) -> RailModel:
    """Build the rail model for the List view's current window.

    ``window_issues`` is the slice of the volume's issues currently
    visible (in volume order). ``arc_members_by_aid`` is the CV-given
    member list for each arc in this volume (with arc reading order
    preserved). ``in_volume_issue_ids`` lets us tell "the next arc
    member is elsewhere in CV" apart from "the next arc member is in
    this volume but outside the window."

    Discontiguous arc segments split into multiple branches sharing
    the same arc/color so each gets its own external-arrow / bend
    treatment at the segment boundaries.
    """
    window_ids = {issue.cv_id for issue in window_issues}
    row_for_cv_id = {issue.cv_id: i for i, issue in enumerate(window_issues)}
    color_for_aid = {aid: arc_hex_for_index(idx) for idx, aid in enumerate(arc_display_order)}

    branches: list[RailBranch] = []

    for aid, members in arc_members_by_aid.items():
        arc = arc_credit_for_aid.get(aid)
        if arc is None:
            continue
        color = color_for_aid.get(aid, _ARC_HEX_PALETTE[0])

        # Walk the arc's full member list and tag each by where it lives.
        member_info = []  # list of (arc_pos, cv_id, is_in_volume, is_in_window)
        for pos, m in enumerate(members):
            mid = m.get("id")
            if mid is None:
                continue
            mid_int = int(mid)
            member_info.append(
                (
                    pos,
                    mid_int,
                    mid_int in in_volume_issue_ids,
                    mid_int in window_ids,
                )
            )

        in_window_entries = [(pos, mid) for (pos, mid, _vol, win) in member_info if win]
        if not in_window_entries:
            continue

        # Build nodes — one per in-window member. ``gap_after`` is True
        # when the arc visits another *volume* between this in-window
        # node and the next. In-volume but out-of-window members
        # between them DON'T count as a gap (the arc stayed put, just
        # outside the visible slice — straight line through).
        nodes: list[RailNode] = []
        for idx, (arc_pos, mid) in enumerate(in_window_entries):
            gap_after = False
            if idx + 1 < len(in_window_entries):
                next_arc_pos = in_window_entries[idx + 1][0]
                # Any out-of-volume member between these two arc
                # positions triggers a bow-out.
                for j in range(arc_pos + 1, next_arc_pos):
                    m = members[j]
                    mid_j = m.get("id")
                    if mid_j is not None and int(mid_j) not in in_volume_issue_ids:
                        gap_after = True
                        break
            nodes.append(
                RailNode(
                    arc_cv_id=aid,
                    row_index=row_for_cv_id[mid],
                    cv_id=mid,
                    gap_after=gap_after,
                )
            )

        # Top edge — based on what (if anything) precedes the first
        # in-window node in the arc.
        first_arc_pos = nodes[0].arc_cv_id  # placeholder, real value below
        first_arc_pos = in_window_entries[0][0]
        any_prev_out_of_volume = any(
            not vol for (pos, _mid, vol, _win) in member_info if pos < first_arc_pos
        )
        any_prev_in_volume = any(
            vol for (pos, _mid, vol, _win) in member_info if pos < first_arc_pos
        )

        top_diverge = False
        top_enters_from_prev_vol = False
        top_fades = False
        if first_arc_pos == 0:
            top_diverge = True
        elif any_prev_out_of_volume:
            top_enters_from_prev_vol = True
        elif any_prev_in_volume:
            top_fades = True
        else:
            top_diverge = True  # defensive — shouldn't reach

        # Bottom edge — mirror.
        last_arc_pos = in_window_entries[-1][0]
        any_next_out_of_volume = any(
            not vol for (pos, _mid, vol, _win) in member_info if pos > last_arc_pos
        )
        any_next_in_volume = any(
            vol for (pos, _mid, vol, _win) in member_info if pos > last_arc_pos
        )

        bottom_merge = False
        bottom_exits_to_next_vol = False
        bottom_fades = False
        if last_arc_pos == len(members) - 1 or (
            not any_next_out_of_volume and not any_next_in_volume
        ):
            bottom_merge = True
        elif any_next_out_of_volume:
            bottom_exits_to_next_vol = True
        elif any_next_in_volume:
            bottom_fades = True

        branches.append(
            RailBranch(
                arc=arc,
                color=color,
                nodes=nodes,
                top_diverge=top_diverge,
                top_enters_from_prev_vol=top_enters_from_prev_vol,
                top_fades=top_fades,
                bottom_merge=bottom_merge,
                bottom_exits_to_next_vol=bottom_exits_to_next_vol,
                bottom_fades=bottom_fades,
            )
        )

    # Lane assignment — split by side. External arcs (anything that
    # touches another volume) get their own lane stack to the LEFT of
    # the spine; internal arcs use the RIGHT stack. Within each side,
    # interval-graph coloring keyed by first-node row.
    branches.sort(key=lambda b: b.nodes[0].row_index)
    left_lanes: list[list[RailBranch]] = []
    right_lanes: list[list[RailBranch]] = []
    for branch in branches:
        target_lanes = left_lanes if branch.is_external else right_lanes
        placed = False
        for lane_idx, lane_branches in enumerate(target_lanes):
            if lane_branches[-1].nodes[-1].row_index < branch.nodes[0].row_index:
                lane_branches.append(branch)
                branch.lane = lane_idx
                placed = True
                break
        if not placed:
            branch.lane = len(target_lanes)
            target_lanes.append([branch])

    num_left_lanes = len(left_lanes)
    num_right_lanes = len(right_lanes)

    # Compute spine_x and width based on lane counts on each side.
    # ``gap_clearance`` is the headroom outside the outermost lane on
    # each side — fits the gap-detour L-shape's horizontal segment
    # plus the arrow tip without clipping.
    gap_clearance = 28
    # Spine sits to the right of all left lanes; min spine_x of 14
    # keeps the volume looking balanced even with no external arcs.
    if num_left_lanes:
        spine_x = num_left_lanes * lane_spacing + gap_clearance + 8
    else:
        spine_x = 14
    # Width accommodates all right lanes + their gap clearance.
    if num_right_lanes:
        right_extent = (num_right_lanes + 1) * lane_spacing + gap_clearance + 8
    else:
        right_extent = 14
    width = spine_x + right_extent

    # Multi-arc rows — store the resolved x-positions directly so the
    # template doesn't need to recompute side-aware coordinates. Each
    # entry is (row_index, sorted list of node x-coords for that row).
    rows_to_xs: dict[int, set[int]] = {}
    for branch in branches:
        if branch.is_external:
            lane_x = spine_x - (branch.lane + 1) * lane_spacing
        else:
            lane_x = spine_x + (branch.lane + 1) * lane_spacing
        for node in branch.nodes:
            rows_to_xs.setdefault(node.row_index, set()).add(lane_x)
    multi_arc_rows = sorted(
        ((row_idx, sorted(xs_set)) for row_idx, xs_set in rows_to_xs.items() if len(xs_set) >= 2),
        key=lambda x: x[0],
    )

    height = len(window_issues) * row_height
    row_y_centers = [int((r + 0.5) * row_height) for r in range(len(window_issues))]

    return RailModel(
        width=width,
        height=height,
        row_height=row_height,
        row_y_centers=row_y_centers,
        spine_x=spine_x,
        lane_spacing=lane_spacing,
        bend_offset=bend_offset,
        branches=branches,
        multi_arc_rows=multi_arc_rows,
    )


def build_gallery_rail(
    visible_segments: list["GallerySegment"],
    all_segments: list["GallerySegment"],
    arc_members_by_aid: dict[int, list[dict]],
    arc_credit_for_aid: dict[int, ArcCredit],
    arc_display_order: list[int],
    in_volume_issue_ids: set[int],
    window_lo: int = 0,
    window_hi: int | None = None,
    cover_height: int = 120,
    cover_gap: int = 8,
    padding_per_nesting: int = 16,
    covers_per_row: int = 8,
    lane_spacing: int = 14,
    bend_offset: int = 16,
) -> RailModel:
    """Build the rail model for the Gallery view — one node per shelf.

    Each visible shelf gets ONE rail row and ONE node per containing arc,
    y-centered on the shelf's vertical middle. Shelves with wrapping
    cover rows (>8 issues) take more vertical space; the rail tracks
    that via per-shelf y-centers in ``row_y_centers``.

    ``window_lo`` / ``window_hi`` describe the issue index range the
    page is currently showing (half-open: ``[window_lo, window_hi)``).
    The rail uses these to compute each shelf's *visible* cover-row
    count — because covers outside the window are ``x-show``ed off in
    the DOM, the actual shelf height shrinks for partially-visible
    segments. Counting full segment size here would drift the rail.

    Branch-appearance rule (spec §4): a branch appears only when the
    arc spans more than one shelf in this volume OR has external
    continuation. Self-contained single-shelf arcs stay silent — the
    gallery shelf container already conveys their full story.
    """
    color_for_aid = {aid: arc_hex_for_index(idx) for idx, aid in enumerate(arc_display_order)}

    if window_hi is None:
        # Default: assume every issue is visible (used for unit tests
        # or callers that don't know the window).
        window_hi = max(
            (seg.last_volume_idx + 1 for seg in visible_segments),
            default=0,
        )

    # Per-shelf metrics: visible cover-row count, nesting depth.
    # ``visible_cover_rows`` counts only the issues actually rendered
    # (those inside [window_lo, window_hi)) — DOM hides others via
    # x-show so the shelf shrinks visually.
    shelf_cover_rows: list[int] = []
    shelf_depth: list[int] = []
    for seg in visible_segments:
        visible_start = max(seg.first_volume_idx, window_lo)
        visible_end = min(seg.last_volume_idx + 1, window_hi)
        visible_count = max(0, visible_end - visible_start)
        cover_rows = max(1, (visible_count + covers_per_row - 1) // covers_per_row)
        # depth = number of nested arc containers around the cover grid.
        # No-arc segments still have one layer of p-2 padding around the
        # ul, so default to 1.
        depth = max(1, len(seg.arcs))
        shelf_cover_rows.append(cover_rows)
        shelf_depth.append(depth)

    # Compute per-shelf y-centers — one row per shelf, centered on
    # that shelf's vertical midpoint (so the node sits in the visual
    # middle of however many cover rows that shelf has).
    row_y_centers: list[int] = []
    y = 0
    for shelf_idx in range(len(visible_segments)):
        cover_rows = shelf_cover_rows[shelf_idx]
        depth = shelf_depth[shelf_idx]
        padding_total = depth * padding_per_nesting
        # Inner content height = cover_rows * cover_height + (cover_rows-1) * cover_gap.
        inner_height = cover_rows * cover_height + max(0, cover_rows - 1) * cover_gap
        shelf_total = padding_total + inner_height
        # Node y-center = shelf top + half of total shelf height.
        row_y_centers.append(int(y + shelf_total / 2))
        y += shelf_total
        # Note: no inter-shelf gap — segments render back-to-back in
        # the DOM with no space-y between them.
    total_height = y

    # Build branches — one node per shelf the arc touches.
    branches: list[RailBranch] = []
    for aid, members in arc_members_by_aid.items():
        arc = arc_credit_for_aid.get(aid)
        if arc is None:
            continue
        color = color_for_aid.get(aid, _ARC_HEX_PALETTE[0])

        arc_member_ids = {int(m["id"]) for m in members if m.get("id") is not None}

        # Branch-appearance rule: spans multiple shelves OR has any
        # out-of-volume member anywhere. Compared against the FULL
        # segment list so window-paginated views render the same set
        # of arcs the user would see if they scrolled to all shelves.
        all_shelves_with_arc: list[int] = []
        for seg_idx, seg in enumerate(all_segments):
            if any(issue.cv_id in arc_member_ids for issue in seg.issues):
                all_shelves_with_arc.append(seg_idx)
        if not all_shelves_with_arc:
            continue
        spans_multiple_shelves = len(all_shelves_with_arc) > 1
        has_external = any(
            m.get("id") is not None and int(m["id"]) not in in_volume_issue_ids for m in members
        )
        if not (spans_multiple_shelves or has_external):
            continue

        # In-window shelves where this arc has presence.
        in_window_shelf_indices: list[int] = []
        for visible_idx, seg in enumerate(visible_segments):
            if any(issue.cv_id in arc_member_ids for issue in seg.issues):
                in_window_shelf_indices.append(visible_idx)
        if not in_window_shelf_indices:
            continue

        # Build nodes — one per containing shelf. ``row_index`` is the
        # shelf's index into ``visible_segments`` (and thus also into
        # ``row_y_centers``).
        nodes: list[RailNode] = []
        prev_shelf_idx: int | None = None
        for shelf_visible_idx in in_window_shelf_indices:
            seg = visible_segments[shelf_visible_idx]

            # Gap detection: between previous shelf's last in-arc issue
            # and this shelf's first in-arc issue, did the arc visit
            # another volume? Mark the previous-shelf node as gap_after
            # so the template draws the L-shape detour.
            if prev_shelf_idx is not None and nodes:
                prev_seg = visible_segments[prev_shelf_idx]
                prev_last_mid = max(
                    (issue.cv_id for issue in prev_seg.issues if issue.cv_id in arc_member_ids),
                    default=None,
                )
                curr_first_mid = min(
                    (issue.cv_id for issue in seg.issues if issue.cv_id in arc_member_ids),
                    default=None,
                )
                if prev_last_mid is not None and curr_first_mid is not None:
                    prev_pos = next(
                        (
                            i
                            for i, m in enumerate(members)
                            if m.get("id") and int(m["id"]) == prev_last_mid
                        ),
                        None,
                    )
                    curr_pos = next(
                        (
                            i
                            for i, m in enumerate(members)
                            if m.get("id") and int(m["id"]) == curr_first_mid
                        ),
                        None,
                    )
                    if prev_pos is not None and curr_pos is not None:
                        between_has_external = any(
                            members[k].get("id") is not None
                            and int(members[k]["id"]) not in in_volume_issue_ids
                            for k in range(prev_pos + 1, curr_pos)
                        )
                        if between_has_external:
                            nodes[-1].gap_after = True

            # Representative cv_id for ARIA / hover — first in-arc
            # issue in this shelf.
            rep_cv_id = next(
                (issue.cv_id for issue in seg.issues if issue.cv_id in arc_member_ids),
                seg.issues[0].cv_id,
            )
            nodes.append(
                RailNode(
                    arc_cv_id=aid,
                    row_index=shelf_visible_idx,
                    cv_id=rep_cv_id,
                )
            )
            prev_shelf_idx = shelf_visible_idx

        # Edge treatments.
        first_arc_pos = next(
            (
                i
                for i, m in enumerate(members)
                if m.get("id") and int(m["id"]) in in_volume_issue_ids
            ),
            None,
        )
        last_arc_pos = next(
            (
                len(members) - 1 - i
                for i, m in enumerate(reversed(members))
                if m.get("id") and int(m["id"]) in in_volume_issue_ids
            ),
            None,
        )
        any_prev_out_of_volume = first_arc_pos is not None and any(
            m.get("id") is not None and int(m["id"]) not in in_volume_issue_ids
            for m in members[:first_arc_pos]
        )
        any_next_out_of_volume = last_arc_pos is not None and any(
            m.get("id") is not None and int(m["id"]) not in in_volume_issue_ids
            for m in members[last_arc_pos + 1 :]
        )

        # Translate visible-shelf indices to global shelf indices to
        # tell whether the arc extends above/below the current window.
        first_in_window_visible = in_window_shelf_indices[0]
        last_in_window_visible = in_window_shelf_indices[-1]
        first_in_window_global = all_segments.index(visible_segments[first_in_window_visible])
        last_in_window_global = all_segments.index(visible_segments[last_in_window_visible])
        arc_extends_above_window = first_in_window_global > all_shelves_with_arc[0]
        arc_extends_below_window = last_in_window_global < all_shelves_with_arc[-1]

        top_diverge = False
        top_enters_from_prev_vol = False
        top_fades = False
        if arc_extends_above_window:
            top_fades = True
        elif any_prev_out_of_volume:
            top_enters_from_prev_vol = True
        else:
            top_diverge = True

        bottom_merge = False
        bottom_exits_to_next_vol = False
        bottom_fades = False
        if arc_extends_below_window:
            bottom_fades = True
        elif any_next_out_of_volume:
            bottom_exits_to_next_vol = True
        else:
            bottom_merge = True

        branches.append(
            RailBranch(
                arc=arc,
                color=color,
                nodes=nodes,
                top_diverge=top_diverge,
                top_enters_from_prev_vol=top_enters_from_prev_vol,
                top_fades=top_fades,
                bottom_merge=bottom_merge,
                bottom_exits_to_next_vol=bottom_exits_to_next_vol,
                bottom_fades=bottom_fades,
            )
        )

    # Lane assignment — same two-stack split as the list rail.
    branches.sort(key=lambda b: b.nodes[0].row_index)
    left_lanes: list[list[RailBranch]] = []
    right_lanes: list[list[RailBranch]] = []
    for branch in branches:
        target_lanes = left_lanes if branch.is_external else right_lanes
        placed = False
        for lane_idx, lane_branches in enumerate(target_lanes):
            if lane_branches[-1].nodes[-1].row_index < branch.nodes[0].row_index:
                lane_branches.append(branch)
                branch.lane = lane_idx
                placed = True
                break
        if not placed:
            branch.lane = len(target_lanes)
            target_lanes.append([branch])

    num_left_lanes = len(left_lanes)
    num_right_lanes = len(right_lanes)
    gap_clearance = 28
    spine_x_final = num_left_lanes * lane_spacing + gap_clearance + 8 if num_left_lanes else 14
    right_extent = (
        (num_right_lanes + 1) * lane_spacing + gap_clearance + 8 if num_right_lanes else 14
    )
    width = spine_x_final + right_extent

    # Multi-arc rows.
    rows_to_xs: dict[int, set[int]] = {}
    for branch in branches:
        if branch.is_external:
            lane_x = spine_x_final - (branch.lane + 1) * lane_spacing
        else:
            lane_x = spine_x_final + (branch.lane + 1) * lane_spacing
        for node in branch.nodes:
            rows_to_xs.setdefault(node.row_index, set()).add(lane_x)
    multi_arc_rows = sorted(
        ((row_idx, sorted(xs_set)) for row_idx, xs_set in rows_to_xs.items() if len(xs_set) >= 2),
        key=lambda x: x[0],
    )

    return RailModel(
        width=width,
        height=total_height,
        row_height=cover_height + cover_gap,  # uniform fallback (unused)
        row_y_centers=row_y_centers,
        spine_x=spine_x_final,
        lane_spacing=lane_spacing,
        bend_offset=bend_offset,
        branches=branches,
        multi_arc_rows=multi_arc_rows,
    )
