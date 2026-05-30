"""Match-review UI routes.

Admin-only surface for confirming, overriding, or re-running the
matcher's PENDING-confidence results. Each route gates on
``RequireAdminDep``; match data is library-wide, so granting review
to every viewer would create conflicting writes."""
