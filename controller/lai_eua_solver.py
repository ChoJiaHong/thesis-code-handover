"""Lai EUA Solver (Baseline 5)
=============================
Per-service First-Fit-Decreasing porting of:
  Lai, He, Abdelrazek, Chen, Hosking, Grundy & Yang (2019) — "Optimal Edge
  User Allocation in Edge Computing with Variable Sized Vector Bin Packing"
  (arXiv:1904.05553)

Original problem: single-service edge user allocation, decision variables
x_ij (user j assigned to server i) and y_i (server i activated), lexicographic
objective — first maximize served users (sum x_ij), then minimize activated
servers (sum y_i) without sacrificing the first objective. The paper's
service provider deploys ONE service across hired servers; there is no
multi-service deployment decision.

Porting notes:
- This thesis is multi-service, so the bin-packing routine is run
  INDEPENDENTLY once per service type: for service T_j, sort its requesters
  and greedily pack them into already-active nodes hosting T_j, activating a
  new node only when no already-active node has room. Different services
  never see each other's packing decisions — this is the deliberate gap this
  baseline illustrates (per-service independent, no user-level bundling),
  mirroring the same porting rationale used for hudson_solver.
- The original paper assumes each server's capacity for the (single) service
  is a fixed number. This thesis instead measures capacity per co-located
  service combo (GPU sharing), so a node's capacity for T_j depends on which
  OTHER services already ended up deployed there by an EARLIER service's
  independent packing pass. This is a real, thesis-specific consequence of
  the per-service-independent porting choice and is deliberately NOT
  corrected: an earlier service's deployment decision does not get
  renegotiated once a later service's pass changes the node's combo. The
  result is exactly the intended illustration — deploy-time estimates of how
  many requesters a node could serve can turn out optimistic once combined
  with what the OTHER independent passes decided, and the final allocation
  (computed after gene is fixed, see lai_eua_allocate) is the ground truth
  that actually reflects this.
- Allocation grants a fixed f_l per served (agent, service) pair rather than
  min(f_h, capacity) — Lai's bin packing is a pure feasibility question (fits
  or doesn't), with no continuous quality notion to reward with extra
  capacity. lsr_solver.py documents the identical design decision for the
  same reason ("either granted its full f_l or not served at all"); this
  follows the same precedent.
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("LAI_EUA")


def lai_eua_bin_pack_deploy(nodes: List[str], services: List[str],
                             subscriptions: List[dict], specs: dict) -> Dict[str, List[str]]:
    """
    Per-service independent First-Fit-Decreasing bin packing.

    For each service (processed independently, in specs['services'] order):
      Step 1 — pack requesters into nodes already hosting the service
               (deployment unchanged, capacity looked up once).
      Step 2 — if requesters remain, activate new nodes one at a time,
               each time re-querying capacity under "current deployment +
               this service newly added to the candidate node" (GPU
               co-location can change what that combo actually yields).

    Returns:
        {node: [service, ...]} — final deployment topology.
    """
    from usc_ts_solver import _get_node_combo_capacity

    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in services}
    cap_cfg = specs.get('nodeCapacity', {})
    default_node_cap = int(cap_cfg.get('default', len(services)))
    deployment: Dict[str, List[str]] = {n: [] for n in nodes}

    for svc in services:  # per-service independent pass
        demand = f_l.get(svc, 0)
        if demand <= 0:
            continue
        n_requesters = sum(
            1 for sub in subscriptions
            if any(e['serviceType'] == svc for e in sub.get('subscriptions', []))
        )
        if n_requesters == 0:
            continue
        remaining = n_requesters

        # Step 1: pack into nodes already hosting svc. Deployment doesn't
        # change here, so capacity only needs to be queried once.
        combo_cap = _get_node_combo_capacity(specs, deployment)
        active_nodes = sorted(
            (n for n in nodes if svc in deployment[n]),
            key=lambda n: -combo_cap.get((n, svc), 0.0),
        )
        for node in active_nodes:
            if remaining <= 0:
                break
            slots = int(combo_cap.get((node, svc), 0.0) // demand)
            remaining -= min(slots, remaining)

        # Step 2: existing nodes can't fit everyone -> activate new nodes.
        while remaining > 0:
            candidates = [
                n for n in nodes
                if svc not in deployment[n]
                and len(deployment[n]) < int(cap_cfg.get(n, default_node_cap))
            ]
            if not candidates:
                break  # no node left to activate; remaining requesters unserved this pass

            best_node, best_cap = None, -1.0
            for n in candidates:
                cand = {k: list(v) for k, v in deployment.items()}
                cand[n] = sorted(cand[n] + [svc])
                c = _get_node_combo_capacity(specs, cand).get((n, svc), 0.0)
                if c > best_cap:
                    best_cap, best_node = c, n

            if best_cap < demand:
                break  # can't even fit one requester's f_l -> not worth activating

            deployment[best_node] = sorted(deployment[best_node] + [svc])
            slots = int(best_cap // demand)
            remaining -= min(slots, remaining)
            logger.debug("[LAI_EUA] activated node=%s for svc=%s cap=%.1f remaining=%d",
                         best_node, svc, best_cap, remaining)

    return deployment


def lai_eua_allocate(env, gene: list, subscriptions: List[dict], specs: dict) -> Dict[Tuple[str, str, str], float]:
    """
    Per-service independent allocation: fixed f_l grant, first-come-first-served
    within each service's requester list, using the FINAL (gene-fixed) combo
    capacity — this is where the true, possibly-reduced capacity from Step 2's
    late-arriving co-located services actually shows up.
    """
    from usc_ts_solver import _get_node_combo_capacity

    services = env.services
    nodes = env.nodes
    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in services}

    deployment: Dict[str, List[str]] = {}
    for ni, node in enumerate(nodes):
        mask = gene[ni]
        combo = sorted([s for si, s in enumerate(services) if mask & (1 << si)])
        if combo:
            deployment[node] = combo

    combo_cap = _get_node_combo_capacity(specs, deployment)
    remaining = dict(combo_cap)

    optimal_x: Dict[Tuple[str, str, str], float] = {}
    for svc in services:  # per-service independent, consistent with deploy phase
        demand = f_l.get(svc, 0)
        if demand <= 0:
            continue
        requesters = sorted(
            f"{sub['agentIP']}:{sub['agentPort']}"
            for sub in subscriptions
            if any(e['serviceType'] == svc for e in sub.get('subscriptions', []))
        )
        for agent_id in requesters:
            candidates = [k for k in combo_cap if k[1] == svc and remaining.get(k, 0) >= demand]
            if not candidates:
                continue
            best = max(candidates, key=lambda k: remaining.get(k, 0))
            optimal_x[(agent_id, best[0], svc)] = demand
            remaining[best] -= demand

    return optimal_x
