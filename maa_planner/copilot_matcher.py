"""Offline experimental matching; no providers, devices or execution side effects."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .operator_box import Operator, OperatorBox
from .prts import CopilotCandidate


@dataclass(frozen=True)
class OperatorIdentity:
    id: str
    name: str
    # Explicit 1-based Copilot indices, never inferred from Box dictionary order.
    skills: dict[int, str] = field(default_factory=dict)
    modules: dict[int, str] = field(default_factory=dict)


class OperatorCatalog:
    """Caller-supplied static identities. Missing/ambiguous mappings stay unknown."""
    def __init__(self, identities: list[OperatorIdentity]):
        self.by_name: dict[str, list[OperatorIdentity]] = {}
        ids = set()
        for item in identities:
            if not item.id or not item.name or item.id in ids:
                raise ValueError("Invalid or duplicate operator identity")
            ids.add(item.id)
            for mapping in (item.skills, item.modules):
                if any(type(k) is not int or k < 1 or not isinstance(v, str) or not v
                       for k, v in mapping.items()) or len(set(mapping.values())) != len(mapping):
                    raise ValueError("Invalid static index mapping")
            self.by_name.setdefault(item.name, []).append(item)

    @classmethod
    def from_battle_data(cls, data: dict, *, skills=None, modules=None):
        """MAA battle_data supplies names only; optional maps must be verified externally."""
        skills, modules = skills or {}, modules or {}
        return cls([OperatorIdentity(key, row['name'], skills.get(key, {}), modules.get(key, {}))
                    for key, row in data['chars'].items()])

    def resolve(self, name: str) -> OperatorIdentity | None:
        matches = self.by_name.get(name, [])
        return matches[0] if len(matches) == 1 else None


def effective_skill_level(operator: Operator, skill_id: str | None) -> int | None:
    base = operator.main_skill_level
    if base is None or base < 7:
        return base
    if skill_id is None or operator.skills is None or skill_id not in operator.skills:
        return None
    mastery = operator.skills[skill_id].mastery
    return None if mastery is None else base + mastery


@dataclass(frozen=True)
class MemberMatch:
    name: str
    operator_id: str | None
    status: str
    missing: bool
    unsatisfied: list[str]
    unknown: list[str]


@dataclass(frozen=True)
class CompatibilityResult:
    copilot_id: int
    status: str
    assignments: dict[str, str]
    support_slot: str | None
    support_operator: str | None
    slots: dict[str, list[MemberMatch]]

    @property
    def support_needed(self) -> bool:
        return self.support_slot is not None

    def to_dict(self) -> dict:
        return dict(asdict(self), support_needed=self.support_needed)


LIMITS = {'elite': 2, 'level': 90, 'skill_level': 10,
          'module': None, 'module_level': 3, 'potential': 6}


def match_member(spec: dict, box: OperatorBox, catalog: OperatorCatalog) -> MemberMatch:
    identity = catalog.resolve(spec['name'])
    unknown, failed = [], []
    requirements = spec.get('requirements', {})
    valid = {}
    for key, value in requirements.items():
        if (key not in LIMITS or type(value) is not int or value < 0
                or (LIMITS[key] is not None and value > LIMITS[key])):
            unknown.append(key)
        else:
            valid[key] = value
    skill = spec.get('skill')
    skill_id = identity.skills.get(skill) if identity else None
    if skill not in (None, 0) and skill_id is None:
        unknown.append('skill_identity')
    module = valid.get('module', 0)
    module_id = identity.modules.get(module) if identity else None
    if module and module_id is None:
        unknown.append('module_identity')
    if valid.get('module_level', 0) and not module:
        unknown.append('module_identity')
    if identity is None:
        unknown.append('operator_identity')
        return MemberMatch(spec['name'], None, 'unknown', False, [], sorted(set(unknown)))
    operator = box.operators.get(identity.id)
    if operator is None:
        return MemberMatch(spec['name'], identity.id, 'unknown' if unknown else 'no',
                           True, ['owned'], sorted(set(unknown)))

    def minimum(key, actual, required):
        if not required:
            return
        if actual is None:
            unknown.append(key)
        elif actual < required:
            failed.append(key)

    for key in ('elite', 'level', 'potential'):
        minimum(key, getattr(operator, key), valid.get(key, 0))
    # Selected skills unlock at E0/E1/E2 respectively, even without skill_level.
    if skill_id is not None:
        minimum('skill_unlock', operator.elite, skill - 1)
    required_skill = valid.get('skill_level', 0)
    actual_skill = (operator.main_skill_level if required_skill <= 7
                    else effective_skill_level(operator, skill_id))
    minimum('skill_level', actual_skill, required_skill)
    if module_id is not None:
        progress = None if operator.modules is None else operator.modules.get(module_id)
        if progress is None:
            unknown.append('module')
        elif progress.unlocked is False:
            failed.append('module_unlock')
        else:
            if progress.unlocked is None:
                unknown.append('module_unlock')
            minimum('module_level', progress.level, max(1, valid.get('module_level', 0)))
    return MemberMatch(spec['name'], identity.id,
                       'no' if failed else ('unknown' if unknown else 'yes'),
                       False, sorted(set(failed)), sorted(set(unknown)))


def _assignment(slots, allowed, *, skip=None, reserved=None):
    """Deterministic augmenting-path bipartite matching, not greedy group choice."""
    owner = {}

    def visit(slot, seen):
        members = sorted(slots[slot], key=lambda m: (m.operator_id or '', m.name))
        for member in members:
            identity = member.operator_id
            if member.status not in allowed:
                continue
            # Unknown names have a shared placeholder, never separate duplicate slots.
            identity = identity or 'unknown:' + member.name
            if identity == reserved or identity in seen:
                continue
            seen.add(identity)
            if identity not in owner or visit(owner[identity], seen):
                owner[identity] = slot
                return True
        return False

    for slot in slots:
        if slot != skip and not visit(slot, set()):
            return None
    return {slot: identity for identity, slot in sorted(owner.items(), key=lambda x: x[1])}


def match_candidate(box: OperatorBox, candidate: CopilotCandidate,
                    catalog: OperatorCatalog) -> CompatibilityResult:
    slots = {f'operator:{i}:{spec["name"]}': [match_member(spec, box, catalog)]
             for i, spec in enumerate(candidate.operators)}
    slots.update({f'group:{i}:{group["name"]}':
                  [match_member(spec, box, catalog) for spec in group['operators']]
                  for i, group in enumerate(candidate.groups)})
    assignment = _assignment(slots, {'yes'})
    if assignment is not None:
        return CompatibilityResult(candidate.id, 'exact', assignment, None, None, slots)

    def support(allowed, certain):
        for slot, members in slots.items():
            for member in sorted(members, key=lambda m: (m.operator_id or '', m.name)):
                if certain and (member.status != 'no' or member.unknown or member.operator_id is None):
                    continue
                identity = member.operator_id or 'unknown:' + member.name
                result = _assignment(slots, allowed, skip=slot, reserved=identity)
                if result is not None:
                    return result, slot, identity
        return None

    supported = support({'yes'}, True)
    if supported is not None:
        assignment, slot, identity = supported
        return CompatibilityResult(candidate.id, 'support_one', assignment, slot, identity, slots)
    # Unresolved data can enable a full assignment or one replacement; never call it exact.
    possible = _assignment(slots, {'yes', 'unknown'})
    uncertain_support = support({'yes', 'unknown'}, False)
    has_unknown = any(m.unknown for members in slots.values() for m in members)
    status = 'unknown' if has_unknown and (possible is not None or uncertain_support is not None) else 'incompatible'
    return CompatibilityResult(candidate.id, status, {}, None, None, slots)


def rank_candidates(box: OperatorBox, candidates: list[CopilotCandidate],
                    catalog: OperatorCatalog) -> list[CompatibilityResult]:
    if len({c.id for c in candidates}) != len(candidates):
        raise ValueError('Duplicate copilot IDs')
    order = {'exact': 0, 'support_one': 1, 'unknown': 2, 'incompatible': 3}

    def score(candidate, key):
        value = candidate.metadata.get(key)
        return value if type(value) in (int, float) and math.isfinite(value) else 0

    pairs = [(candidate, match_candidate(box, candidate, catalog)) for candidate in candidates]
    pairs.sort(key=lambda pair: (order[pair[1].status],
                                *(-score(pair[0], key) for key in
                                  ('hot_score', 'rating_level', 'rating_ratio', 'views')),
                                pair[0].id))
    return [result for _, result in pairs]
