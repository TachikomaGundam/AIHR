from __future__ import annotations

SEATS = ["oracle", "ultrabrain", "metis", "deep", "momus", "prometheus"]
ITEMS = []

def register(tier, slug, question, answer_kind, ref, xcheck=None,
             checkpoints=None, multi_step_state=False, tolerance=None,
             seats_override=None, canary=False, extra_meta=None):
    if checkpoints is None: checkpoints=[]
    if seats_override is None:
        idx=len(ITEMS); seats=[SEATS[idx%len(SEATS)],SEATS[(idx+1)%len(SEATS)]]
    else: seats=seats_override
    ITEMS.append({"tier":tier,"slug":slug,"question":question,"answer_kind":answer_kind,
        "ref":ref,"xcheck":xcheck,"checkpoints":checkpoints,
        "multi_step_state":multi_step_state,"tolerance":tolerance,
        "seats":seats,"canary":canary,"extra_meta":extra_meta or {}})
