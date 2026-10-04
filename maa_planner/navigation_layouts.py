"""Evidence-scoped UI adapters; these never decide which stages are playable."""
from __future__ import annotations


STANDARD = 'standard'
DV_SPECIAL_ACCESS = 'dv_special_access'
NL_SPECIAL_ROOK = 'nl_special_rook'

# Only the archived editions and sections with observed layouts are adapted.
# Catalog routing remains data-driven for every other stage/event. A code
# substring, free entry, translated label or caller-provided layout is no proof.
_ARCHIVE_LAYOUTS = {
    ("permanent_sidestory_15_Dorothy's_Vision", 'permanent_sidestory_15_zone1'): (
        DV_SPECIAL_ACCESS, {f'DV-S-{n}': f'act19side_s0{n}' for n in (1, 2)}),
    ('permanent_sidestory_10_Near_Light', 'permanent_sidestory_10_zone3'): (
        NL_SPECIAL_ROOK, {f'NL-S-{n}': f'act13side_s0{n}' for n in range(1, 6)}),
}


def navigation_layout(route: dict | None) -> str:
    if not isinstance(route, dict) or route.get('kind') != 'archive':
        return STANDARD
    raid = route.get('raid', False)
    if type(raid) is not bool:
        return STANDARD
    adapter = _ARCHIVE_LAYOUTS.get((route.get('activity_id'), route.get('zone_id')))
    if adapter is None:
        return STANDARD
    layout, stages = adapter
    battle = stages.get(route.get('code'))
    if battle is None or (layout == DV_SPECIAL_ACCESS and raid):
        return STANDARD
    expected = battle + ('#f#' if raid else '')
    if route.get('battle_id') != expected or route.get('stage_id') != expected:
        return STANDARD
    return layout


def stage_panel_tasks(route: dict | None) -> list[str]:
    panels = ['ZootdStagePanel']
    if navigation_layout(route) == DV_SPECIAL_ACCESS:
        panels.append('ZootdSpecialPanel')
    return panels + ['ZootdMapReady']
