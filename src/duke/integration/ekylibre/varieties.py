"""Minimal Ekylibre variety ancestry for routing tools/doers to Procedo slots.

Procedo parameter filters (e.g. `is motorized_vehicle and can tow(equipment)`)
test a product's *variety* against the Onoma variety nomenclature, which is a
tree (`tractor` → `motorized_vehicle` → `equipment`). That tree lives in the
`onoma` gem (`db/reference.xml`, `<item name=… parent=…/>`), not in the tenant
DB or any API, so we embed the small, stable sub-tree we actually need to route
intervention tools and doers.

We deliberately do NOT replicate Procedo's full filter engine: the `can …(…)`
ability clauses depend on per-variety ability tables that would have to be
ported and kept in sync. Routing on the `is <variety>` clause alone is enough to
separate motorized vehicles (tractors, self-propelled) from towed/portable
implements and from tanks — the distinction that actually changes which slot a
tool belongs to. See `procedure_registry.select_parameter_name`.

Source: ekylibre-plugins/onoma/db/reference.xml (equipment + worker subtrees).
These core varieties have been stable for years; if Ekylibre adds a new
equipment variety we don't know about, ancestry simply stops at the unknown
node and the caller falls back to the default slot — never a wrong guess.
"""

from __future__ import annotations

# child -> parent. Roots (`equipment`, `worker`) intentionally absent.
_VARIETY_PARENTS: dict[str, str] = {
    # motorized vehicles (self-propelled) — the `is motorized_vehicle` branch
    "motorized_vehicle": "equipment",
    "tractor": "motorized_vehicle",
    "car": "motorized_vehicle",
    "truck": "motorized_vehicle",
    "handling_equipment": "motorized_vehicle",
    "heavy_equipment": "motorized_vehicle",
    "self_propelled_equipment": "motorized_vehicle",
    # other equipment branches (towed / portable / fixed / storage)
    "trailed_equipment": "equipment",
    "portable_equipment": "equipment",
    "fixed_equipment": "equipment",
    "connected_object": "equipment",
    "tank": "equipment",
}


def variety_distance(variety: str | None, ancestor: str) -> int | None:
    """Steps from `variety` up to `ancestor` in the variety tree.

    Returns 0 when they are equal, N when `ancestor` is the Nth parent, and
    None when `variety` is not `ancestor` nor any descendant of it (or is
    unknown). Lower = more specific match, which the slot selector uses to
    prefer `motorized_vehicle` over the broader `equipment` for a tractor.
    """
    if not variety:
        return None
    current = variety
    distance = 0
    seen: set[str] = set()
    while current and current not in seen:
        if current == ancestor:
            return distance
        seen.add(current)
        parent = _VARIETY_PARENTS.get(current)
        if parent is None:
            return None
        current = parent
        distance += 1
    return None
