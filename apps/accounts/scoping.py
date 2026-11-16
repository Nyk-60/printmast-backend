"""Resolves "whose data is this" for a request — the one place that knows
about both a real sub-user and a Team Member (a sub-user-created, store-
restricted login, see TeamMemberProfile). Every view that used to do
`request.user.sub_user_profile` directly to scope a queryset should go
through get_sub_user_scope instead, so a Team Member's store restriction
can never be silently skipped at a call site that forgot about it."""


def get_sub_user_scope(user):
    """Returns (sub_user_profile, store_ids_or_None).

    store_ids_or_None is None for a real sub-user (no restriction — they see
    all their own stores) or the set of Store ids a Team Member is assigned
    to (apply as `.filter(store_id__in=store_ids)` wherever the queryset is
    store-aware). Returns (None, None) for anyone else (PC, superadmin, an
    inactive Team Member, or an unauthenticated/anonymous user)."""
    sub_user = getattr(user, 'sub_user_profile', None)
    if sub_user is not None:
        return sub_user, None
    team_member = getattr(user, 'team_member_profile', None)
    if team_member is not None and team_member.is_active:
        store_ids = set(team_member.assigned_stores.values_list('id', flat=True))
        return team_member.sub_user, store_ids
    return None, None
