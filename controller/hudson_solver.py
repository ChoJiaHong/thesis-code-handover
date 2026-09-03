"""Hudson Solver (Baseline 4)
===========================
Greedy marginal-gain porting of:
  Hudson, Khamfroush & Lucani (2021) — "QoS-Aware Placement of Deep Learning
  Services on the Edge with Multiple Service Implementations" (arXiv:2104.15094)

Original AGP algorithm: greedily place (edge, service, model-implementation)
tuples in decreasing order of marginal QoS gain across all outstanding
requests, where Q(u, s_u, m) averages an accuracy-satisfaction score and a
latency-satisfaction score for each request independently; terminates when no
placement yields positive marginal gain (source of the paper's (1-1/e)
submodular approximation guarantee). The paper explicitly represents a
multi-service user as separate, independent request entries rather than a
single user-level bundle.

Porting notes:
- This thesis's services have a single implementation each (no
  accuracy/latency-tradeoff model selection), so the "choose which model
  implementation" dimension of the original AGP is dropped. What is kept is
  the core mechanism: greedy placement by marginal count of newly-satisfiable
  requests, and per-(agent, service) independent satisfaction judgement.
- Q(u, s_u, m) is simplified to a binary per-(agent, service) threshold
  indicator (does the pair reach f_l), the same threshold semantics this
  thesis's own Problem P uses for a single service — but, faithfully to the
  source paper, WITHOUT bundling a user's multiple subscribed services into
  one joint pass/fail judgement. This is the deliberate gap this baseline is
  meant to illustrate, not an oversight.
- Capacity is looked up via usc_ts_solver._get_node_alone_capacity — the
  single-service-alone workAbility value, NOT this thesis's own GPU
  co-location combo capacity table. The source paper's own resource model
  (Eq. 5-6: D^comp_sm(u) = w_sm/(W_e/|U_e|)) evenly divides an edge cloud's
  capacity across its covered users with no notion of cross-service capacity
  coupling ("an edge cloud's computation capacity is evenly shared across its
  covered users") — it has no concept of GPU co-location shrinking a
  service's throughput based on what else shares the node. Querying this
  thesis's own combo-aware capacity table would hand AGP information its
  1:1-original design never had access to and could never have produced
  itself; using the alone-capacity keeps the port honest to what the source
  paper's own resource assumption actually is. A direct, faithful consequence
  is that this baseline's placement/allocation decisions can over-commit
  co-located nodes — each service is planned as if it alone owns the node's
  full capacity — and real measured throughput can fall short of what was
  allocated once multiple co-located services are actually served
  concurrently. This is not a porting defect; it is what deploying a
  co-location-unaware algorithm onto a real GPU-shared system predicts.
- Marginal gain is still measured as the change in the TOTAL satisfied-count
  summed across all services, not just the newly-placed service's own count.
  Since capacity no longer shrinks with co-location, there is no crowding-out
  effect left to fold in by summing — the aggregate-sum form is kept simply
  because it is the faithful reading of AGP's actual objective, σ(P) in the
  source paper, which is already defined as a global sum over all users'
  Q-scores, not a per-service count restricted to the newly-placed service.
- Allocation grants min(f_h, remaining capacity) once a pair clears f_l,
  mirroring DL3's allocator rather than LSR's fixed-f_l grant, because Q in
  the source paper is a continuous satisfaction score (not a pure
  feasibility check like Lai's bin packing). "Remaining capacity" here is
  each (node, service)'s alone-capacity, decremented in subscription-list
  order as pairs are granted — first-come-first-served against a fixed,
  co-location-unaware ceiling, not a literal even split of that ceiling
  across simultaneous requesters.
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("HUDSON")


def hudson_greedy_deploy(nodes: List[str], services: List[str],
                          subscriptions: List[dict], specs: dict) -> Dict[str, List[str]]:
    """
    AGP-style greedy marginal-gain deployment.

    Each round, evaluates every (node, service) placement not yet made and
    picks the one that lets the most currently-unsatisfied (agent, service)
    pairs reach f_l, given the capacity that placement would produce under
    GPU co-location. Stops when no candidate yields a positive gain.

    Returns:
        {node: [service, ...]} — final deployment topology.
    """
    from usc_ts_solver import _get_node_alone_capacity

    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in services}
    # Alone-capacity is topology-independent (unlike combo capacity), so it is
    # computed once up front rather than re-queried for every candidate.
    alone_cap = _get_node_alone_capacity(specs, nodes, services)

    n_req: Dict[str, int] = {s: 0 for s in services}
    for sub in subscriptions:
        for e in sub.get('subscriptions', []):
            if e['serviceType'] in n_req:
                n_req[e['serviceType']] += 1

    cap_cfg = specs.get('nodeCapacity', {})
    default_node_cap = int(cap_cfg.get('default', len(services)))
    deployment: Dict[str, List[str]] = {n: [] for n in nodes}

    def _sat_counts(deploy_dict: Dict[str, List[str]]) -> Dict[str, int]:
        """Per-service count of requests that could reach f_l under deploy_dict."""
        total_cap: Dict[str, float] = {s: 0.0 for s in services}
        for node, svcs_on_node in deploy_dict.items():
            for s in svcs_on_node:
                total_cap[s] += alone_cap.get((node, s), 0.0)
        return {
            s: (min(int(total_cap[s] // f_l[s]), n_req[s]) if f_l.get(s, 0) > 0 else n_req[s])
            for s in services
        }

    cur_sat = _sat_counts(deployment)

    while True:
        best_gain, best_node, best_deploy, best_sat = 0, None, None, None
        for node in nodes:
            if len(deployment[node]) >= int(cap_cfg.get(node, default_node_cap)):
                continue
            for svc in services:
                if svc in deployment[node]:
                    continue
                # Independent copy: candidates within this round must be
                # compared against the same baseline, not against each other's
                # tentative placements.
                cand = {n: list(v) for n, v in deployment.items()}
                cand[node] = sorted(cand[node] + [svc])
                cand_sat = _sat_counts(cand)
                # 用全域總和的邊際增量，不是只看新增服務自己的計數——容量已
                # 改為單服務獨立容量，不再隨共置變化，同節點服務間已無排擠
                # 效果需要折算；維持全域加總純粹是忠實對應 Hudson 原文
                # σ(P) 本身即為跨全體使用者 Q 值加總的定義，非新增服務
                # 自身計數。
                gain = sum(cand_sat.values()) - sum(cur_sat.values())
                if gain > best_gain:
                    best_gain, best_node = gain, node
                    best_deploy, best_sat = cand, cand_sat

        if best_gain <= 0:
            break
        deployment, cur_sat = best_deploy, best_sat
        logger.debug("[HUDSON] placed marginal gain=%d at node=%s -> deployment=%s",
                     best_gain, best_node, deployment)

    return deployment


def hudson_allocate(env, gene: list, subscriptions: List[dict], specs: dict) -> Dict[Tuple[str, str, str], float]:
    """
    Per-(agent, service) independent allocation: grants min(f_h, remaining
    capacity) once a pair reaches f_l, without checking whether the same
    agent's other subscribed services are also satisfied.
    """
    from usc_ts_solver import _get_node_alone_capacity

    services = env.services
    nodes = env.nodes
    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in services}
    f_h = {s: specs['services'][s]['frequencyLimit'][0] for s in services}

    deployment: Dict[str, List[str]] = {}
    for ni, node in enumerate(nodes):
        mask = gene[ni]
        combo = sorted([s for si, s in enumerate(services) if mask & (1 << si)])
        if combo:
            deployment[node] = combo

    # Alone-capacity, restricted to (node, service) pairs actually deployed —
    # a service not placed at a node is not a valid allocation candidate there
    # regardless of what its alone-capacity value would be.
    alone_cap_all = _get_node_alone_capacity(specs, nodes, services)
    combo_cap = {(node, s): alone_cap_all[(node, s)] for node, combo in deployment.items() for s in combo}
    remaining = dict(combo_cap)

    optimal_x: Dict[Tuple[str, str, str], float] = {}
    for sub in subscriptions:
        agent_id = f"{sub['agentIP']}:{sub['agentPort']}"
        for e in sub.get('subscriptions', []):
            svc = e['serviceType']
            need = f_l.get(svc, 0)
            candidates = [k for k in combo_cap if k[1] == svc and remaining.get(k, 0) >= need]
            if not candidates:
                continue
            best = max(candidates, key=lambda k: remaining.get(k, 0))
            freq = min(f_h.get(svc, 0), remaining[best])
            optimal_x[(agent_id, best[0], svc)] = freq
            remaining[best] -= freq

    return optimal_x
