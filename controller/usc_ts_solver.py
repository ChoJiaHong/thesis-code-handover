"""
USC + USC-TS + Round-Robin LB Solver
======================================
Independent baseline comparison system implementing:

  Algorithm 1 – USC  (User-oriented Service Caching)
    Per-node 0/1 knapsack DP to decide which services to cache on each node.
    Time complexity: O(|M| × |S| × (R̂+1))

  Algorithm 2 – USC-TS (USC based on Tabu Search)
    Multi-node tabu search starting from USC's solution to reduce
    redundant replicas and better utilise adjacent server resources.
    Time complexity: O(Γ × |M| × |S|)

  Round-Robin Load Balancer
    Assigns agents to service replicas in round-robin order so that
    traffic is distributed evenly across all deployed replicas.

Design assumptions
------------------
* All nodes have the same processing capacity (homogeneous).
  Capacity values are read from information/usc_ts_config.json
  (uniformCapacity), which defaults to the minimum single-service
  throughput observed across all nodes in workAbility.
* x[m][s] ∈ {0, 1}: a service is either deployed or not on a node.
  Multiple nodes deploying the same service = multiple replicas.
* nodeCapacity (R̂) = maximum number of distinct service types a
  single node may host simultaneously (default 2).

Reference: "Enhanced Multi-Phase Optimisation Framework for MEC"
"""

import copy
import json
import logging
import math
import os
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
USC_TS_CONFIG_FILE = os.path.join(_DIR, "information", "usc_ts_config.json")


# ===========================================================================
# Internal helpers
# ===========================================================================

def _load_usc_ts_config() -> dict:
    """Load USC-TS-specific config (uniformCapacity, nodeCapacity)."""
    try:
        with open(USC_TS_CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not load USC-TS config: %s", exc)
        return {}


def _merge_specs(specs: dict) -> dict:
    """
    Return a copy of *specs* enriched with fields from usc_ts_config.json.
    Fields in the config file take precedence over anything already in specs.
    """
    merged = copy.deepcopy(specs)
    cfg = _load_usc_ts_config()
    if "uniformCapacity" in cfg:
        merged["uniformCapacity"] = cfg["uniformCapacity"]
    if "nodeCapacity" in cfg:
        merged["nodeCapacity"] = cfg["nodeCapacity"]
    return merged


def _get_uniform_capacity(specs: dict) -> Dict[str, float]:
    """
    Returns per-service uniform node capacity (fps).

    Priority order:
      1. specs['uniformCapacity']  (set by usc_ts_config.json)
      2. Minimum single-service throughput across all nodes in workAbility
      3. Hard-coded fallback of 100 fps
    """
    if "uniformCapacity" in specs:
        return dict(specs["uniformCapacity"])

    services = list(specs.get("services", {}).keys())
    work_ability = specs.get("workAbility", {})
    cap: Dict[str, float] = {}
    for s in services:
        values = []
        for node_data in work_ability.values():
            # single-service combo key equals the service name
            if s in node_data and s in node_data[s]:
                values.append(node_data[s][s])
        cap[s] = min(values) if values else 100.0
    return cap


def _svc_combo_key(svcs: list, specs: dict) -> str:
    """
    Build a workAbility combo key using the canonical service order defined in
    specs['services'], not alphabetical order.
    e.g. ['object','gesture','pose'] → 'gesture,pose,object'
    """
    order = list(specs.get("services", {}).keys())
    return ",".join(s for s in order if s in svcs)


def _get_node_combo_capacity(specs: dict, topology: Dict[str, List[str]]) -> Dict[Tuple[str, str], float]:
    """
    Return actual FPS capacity for each (node, service) pair based on the
    exact service combo currently deployed on that node via workAbility lookup.

    Example: node deploys ["gesture", "pose"]
      combo_key = "gesture,pose"  (ordered by specs['services'] definition)
      → {(node, "gesture"): 103.0, (node, "pose"): 69.0}
    """
    result: Dict[Tuple[str, str], float] = {}
    work_ability = specs.get("workAbility", {})
    for node, svcs_on_node in topology.items():
        if not svcs_on_node:
            continue
        combo_key = _svc_combo_key(svcs_on_node, specs)
        node_data = work_ability.get(node, {})
        combo_data = node_data.get(combo_key, {})
        if combo_data:
            for s in svcs_on_node:
                result[(node, s)] = float(combo_data.get(s, 0.0))
        else:
            for s in svcs_on_node:
                result[(node, s)] = float(node_data.get(s, {}).get(s, 100.0))
    return result


def _get_node_alone_capacity(specs: dict, nodes: List[str], services: List[str]) -> Dict[Tuple[str, str], float]:
    """
    Return single-service-alone FPS capacity for every (node, service) pair —
    the workAbility[node][svc][svc] entry — regardless of what else is or
    might be co-located on that node. Unlike _get_node_combo_capacity, this
    does NOT depend on topology: the value for (node, svc) is constant no
    matter what other services share the node.

    This mirrors the resource assumption of literature baselines whose
    original papers have no GPU co-location concept — e.g. Hudson et al.
    (2021)'s delay model divides an edge cloud's capacity evenly across its
    covered users with no notion of cross-service capacity coupling ("an edge
    cloud's computation capacity is evenly shared across its covered users").
    Giving such a baseline this thesis's own co-location-aware combo capacity
    table would hand it information its original design never had access to.
    """
    work_ability = specs.get("workAbility", {})
    result: Dict[Tuple[str, str], float] = {}
    for node in nodes:
        node_data = work_ability.get(node, {})
        for s in services:
            result[(node, s)] = float(node_data.get(s, {}).get(s, 100.0))
    return result


def _get_node_capacity(specs: dict, nodes: list) -> Dict[str, int]:
    """
    Returns R̂_m: maximum number of service types per node.

    Reads from specs['nodeCapacity']:
      {"default": 2, "node4": 3, ...}
    Missing nodes receive the default value (2 if unspecified).
    """
    cfg = specs.get("nodeCapacity", {})
    default = int(cfg.get("default", 2))
    return {n: int(cfg.get(n, default)) for n in nodes}


def _service_weights(specs: dict) -> Dict[str, int]:
    """Returns GPU memory units required per service (default 1)."""
    return {
        s: int(data.get("gpuMemoryRequest", 1))
        for s, data in specs.get("services", {}).items()
    }


# ===========================================================================
# Service value & minimum replica bounds
# ===========================================================================

def compute_service_value(
    services: list,
    specs: dict,
    demand: Dict[str, int],
) -> Dict[str, float]:
    """
    Compute the knapsack item value Ψ_s for each service.

        Ψ_s = d_s × ⌊ C_s / f_l^s ⌋

    Interpretation: deploying service s on one node can support at most
    ⌊C_s / f_l⌋ agents at minimum QoS; scaled by actual demand d_s to
    reflect how much benefit the deployment brings.

    Services with zero demand receive Ψ = 0 so the knapsack ignores them.
    """
    cap = _get_uniform_capacity(specs)
    psi: Dict[str, float] = {}
    for s in services:
        d_s = demand.get(s, 0)
        if d_s == 0:
            psi[s] = 0.0
            continue
        f_l = specs["services"][s]["frequencyLimit"][1]  # lower bound
        c_s = cap.get(s, 100.0)
        psi[s] = float(d_s * (math.floor(c_s / f_l) if f_l > 0 else 1))
    return psi


def compute_min_replicas(
    services: list,
    specs: dict,
    demand: Dict[str, int],
) -> Dict[str, int]:
    """
    Compute the minimum number of replicas needed to serve all agents at the
    lowest acceptable QoS.

        n_s^min = max(1, ⌈ d_s × f_l^s / C_s ⌉)

    Services with zero demand require 0 replicas.
    """
    cap = _get_uniform_capacity(specs)
    n_min: Dict[str, int] = {}
    for s in services:
        d_s = demand.get(s, 0)
        if d_s == 0:
            n_min[s] = 0
            continue
        f_l = specs["services"][s]["frequencyLimit"][1]
        c_s = cap.get(s, 100.0)
        n_min[s] = max(1, math.ceil(d_s * f_l / c_s))
    return n_min


# ===========================================================================
# Algorithm 1: USC – per-node 0/1 knapsack DP
# ===========================================================================

def usc_single_node(
    services: list,
    psi: Dict[str, float],
    weights: Dict[str, int],
    R_hat: int,
) -> List[str]:
    """
    Solve the service-caching problem for ONE edge node via 0/1 knapsack DP.

    Each service s is treated as a knapsack item:
      value  = psi[s]       (Ψ_s)
      weight = weights[s]   (GPU memory units; typically 1)

    Capacity = R_hat (maximum GPU units / service-slot count for this node).

    Time complexity: O(|S| × R̂)

    Returns
    -------
    List of service names selected for deployment on this node.
    """
    n = len(services)
    if n == 0 or R_hat <= 0:
        return []

    # dp[i][j] = best total value using the first i items with capacity j
    dp = [[0.0] * (R_hat + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        s = services[i - 1]
        w = weights.get(s, 1)
        v = psi.get(s, 0.0)
        for j in range(R_hat + 1):
            dp[i][j] = dp[i - 1][j]           # skip item i
            if j >= w and dp[i - 1][j - w] + v > dp[i][j]:
                dp[i][j] = dp[i - 1][j - w] + v  # include item i

    # Backtrack to recover the selected set
    selected: List[str] = []
    j = R_hat
    for i in range(n, 0, -1):
        if dp[i][j] != dp[i - 1][j]:
            s = services[i - 1]
            selected.append(s)
            j -= weights.get(s, 1)

    return selected


# ===========================================================================
# Algorithm 2: USC-TS – multi-node tabu search
# ===========================================================================

def _count_replicas(x: Dict[str, set]) -> Dict[str, int]:
    """Return total replica count per service across all nodes."""
    n_deploy: Dict[str, int] = {}
    for svcs_on_node in x.values():
        for s in svcs_on_node:
            n_deploy[s] = n_deploy.get(s, 0) + 1
    return n_deploy


def _evaluate(
    x: Dict[str, set],
    psi: Dict[str, float],
    n_min: Dict[str, int],
    alpha: float,
    beta: float = 100.0,
) -> float:
    """
    Objective function:

        F = Σ_{m,s} x[m][s] · Ψ_s
            − α · Σ_s max(0, n_s^deploy − n_s^min)   [redundancy penalty]
            − β · Σ_s max(0, n_s^min  − n_s^deploy)   [coverage deficit penalty]

    β >> α ensures that covering all demanded services takes strict priority
    over eliminating redundant replicas.  Without the β term, the initial USC
    solution (which may leave some services at 0 replicas) looks locally
    optimal, and the tabu search never repairs coverage.
    """
    n_deploy = _count_replicas(x)
    reward = sum(psi.get(s, 0.0) for svcs in x.values() for s in svcs)
    excess = sum(
        max(0, n_deploy.get(s, 0) - n_min.get(s, 0))
        for s in psi
    )
    # Services with n_min > 0 that have too few replicas are strongly penalised
    under = sum(
        max(0, n_min.get(s, 0) - n_deploy.get(s, 0))
        for s in n_min
        if n_min[s] > 0
    )
    return reward - alpha * excess - beta * under


def usc_tabu_search(
    init_x: Dict[str, list],
    nodes: list,
    services: list,
    psi: Dict[str, float],
    n_min: Dict[str, int],
    R_hat_per_node: Dict[str, int],
    weights: Dict[str, int],
    max_iter: int = 50,
    tabu_size: int = 10,
    alpha: float = 1.0,
    beta: float = 100.0,
) -> Dict[str, List[str]]:
    """
    Refine a USC initial deployment via tabu search across all edge nodes.

    Neighbourhood moves
    -------------------
    ADD    (m, s): Deploy service s on node m.
                   Valid: total weight on m after add ≤ R̂_m.
    REMOVE (m, s): Remove service s from node m.
                   Valid: remaining replicas of s ≥ n_min[s]
                   (coverage of s is still satisfied).

    Tabu list
    ---------
    Stores the last *tabu_size* toggled (node, service) pairs (FIFO).
    A tabu move can still be accepted if it beats the best-so-far score
    (aspiration criterion).

    Time complexity: O(Γ × |M| × |S|)

    Returns
    -------
    Best deployment matrix found: {node: sorted list of service names}
    """
    # Use sets for O(1) membership tests during the search
    current_x: Dict[str, set] = {m: set(svcs) for m, svcs in init_x.items()}
    best_x = copy.deepcopy(current_x)
    best_score = _evaluate(current_x, psi, n_min, alpha, beta)

    tabu_list: List[Tuple[str, str]] = []

    for _iter in range(max_iter):
        n_deploy = _count_replicas(current_x)
        best_move: Tuple = None     # type: ignore[assignment]
        best_move_score = float("-inf")

        for m in nodes:
            current_weight = sum(weights.get(s, 1) for s in current_x[m])

            for s in services:
                if psi.get(s, 0.0) == 0.0:
                    continue  # zero-demand services are never worth deploying

                deployed = s in current_x[m]
                move = (m, s)

                if deployed:
                    # REMOVE move: valid only if coverage stays satisfied
                    if n_deploy.get(s, 0) - 1 < n_min.get(s, 0):
                        continue
                    current_x[m].discard(s)
                    score = _evaluate(current_x, psi, n_min, alpha, beta)
                    current_x[m].add(s)          # restore

                else:
                    # ADD move: valid only if node capacity is not exceeded
                    if current_weight + weights.get(s, 1) > R_hat_per_node.get(m, 2):
                        continue
                    current_x[m].add(s)
                    score = _evaluate(current_x, psi, n_min, alpha, beta)
                    current_x[m].discard(s)      # restore

                is_tabu = move in tabu_list
                # Accept if not tabu, OR tabu but beats global best (aspiration)
                if (not is_tabu or score > best_score) and score > best_move_score:
                    best_move_score = score
                    best_move = (m, s, deployed)

        if best_move is None:
            break  # no valid or admissible move found

        m_star, s_star, was_deployed = best_move
        if was_deployed:
            current_x[m_star].discard(s_star)
        else:
            current_x[m_star].add(s_star)

        # Update tabu list (FIFO)
        tabu_list.append((m_star, s_star))
        if len(tabu_list) > tabu_size:
            tabu_list.pop(0)

        # Update global best if improved
        current_score = _evaluate(current_x, psi, n_min, alpha, beta)
        if current_score > best_score:
            best_score = current_score
            best_x = copy.deepcopy(current_x)

    logger.info(
        "USC-TS done. best_score=%.2f, max_iter=%d, tabu_size=%d",
        best_score, max_iter, tabu_size,
    )
    return {m: sorted(svcs) for m, svcs in best_x.items()}


# ===========================================================================
# Round-Robin Load Balancer
# ===========================================================================

def round_robin_lb(
    topology: Dict[str, List[str]],
    subscriptions: list,
    specs: dict,
) -> Tuple[list, list]:
    """
    Distribute agents across service replicas using round-robin routing.

    Algorithm
    ---------
    For each service s:
      1. Collect replica nodes  R_s = {m | s ∈ topology[m]}  (already sorted)
      2. Assign each requesting agent to R_s[i % |R_s|]  (round-robin)
      3. Compute per-agent frequency:
             freq = min(f_h^s,  max(f_l^s,  C_s / agents_on_this_replica))

    C_s uses _get_node_combo_capacity (real measured throughput W(i,c,j) for
    the actual service combo deployed on each node). The deployment decision
    itself (USC / USC-TS DP + Tabu Search) still reasons only in terms of the
    uniform, non-combo capacity via _get_uniform_capacity() — unchanged from
    the original EMS paper's spirit, which never models co-location
    contention when deciding placement. Only the routing/frequency layer is
    grounded in the real capacity table, so the comparison against this
    research's own method and LSR (which already uses the same W(i,c,j)
    table) is not confounded by EMS using a cruder capacity estimate.

    Port values are set to 0 (placeholder); the caller fills them in.

    Returns
    -------
    (target_topology, allocation)
      target_topology : list of {"nodeName", "serviceType", "hostPort": 0}
      allocation      : list of {"agentIP", "agentPort", "targetNode",
                                  "serviceType", "hostPort": 0, "frequency"}
    """
    combo_cap = _get_node_combo_capacity(specs, topology)
    f_limits: Dict[str, list] = {
        s: data["frequencyLimit"]
        for s, data in specs.get("services", {}).items()
    }

    # Build replica list per service (sorted for determinism)
    replicas: Dict[str, List[str]] = {}
    for node, svcs_on_node in sorted(topology.items()):
        for s in svcs_on_node:
            replicas.setdefault(s, []).append(node)

    # Round-robin counters and load tracking
    rr_idx: Dict[str, int] = {s: 0 for s in replicas}
    replica_load: Dict[Tuple[str, str], int] = {}  # (node, service) → agent count
    # raw assignment list
    raw_assignments: List[dict] = []

    for sub in subscriptions:
        ag_ip = sub["agentIP"]
        ag_port = sub["agentPort"]
        for s_entry in sub.get("subscriptions", []):
            s_type = s_entry["serviceType"]
            if s_type not in replicas or not replicas[s_type]:
                logger.warning("No replica available for service %s", s_type)
                continue

            target_node = replicas[s_type][rr_idx[s_type] % len(replicas[s_type])]
            rr_idx[s_type] += 1

            key = (target_node, s_type)
            replica_load[key] = replica_load.get(key, 0) + 1
            raw_assignments.append({
                "agentIP": ag_ip,
                "agentPort": int(ag_port),
                "serviceType": s_type,
                "targetNode": target_node,
            })

    # Build target_topology
    target_topology: list = []
    seen: set = set()
    for node, svcs_on_node in sorted(topology.items()):
        for s in svcs_on_node:
            if (node, s) not in seen:
                seen.add((node, s))
                target_topology.append({
                    "nodeName": node,
                    "serviceType": s,
                    "hostPort": 0,
                })

    # Build allocation with computed frequencies
    allocation: list = []
    for asgn in raw_assignments:
        s_type = asgn["serviceType"]
        key = (asgn["targetNode"], s_type)
        n_agents = replica_load.get(key, 1)
        c_s = combo_cap.get(key, 100.0)
        f_h = f_limits.get(s_type, [25, 10])[0]   # upper bound
        f_l = f_limits.get(s_type, [25, 10])[1]   # lower bound
        freq = round(min(f_h, max(f_l, c_s / n_agents)), 2)
        allocation.append({
            "agentIP": asgn["agentIP"],
            "agentPort": asgn["agentPort"],
            "targetNode": asgn["targetNode"],
            "serviceType": s_type,
            "hostPort": 0,
            "frequency": freq,
        })

    return target_topology, allocation


# ===========================================================================
# Main entry point
# ===========================================================================

def trigger_solver_usc_ts(
    subscriptions: list,
    svcs: list,
    nodes_status: dict,
    specs: dict,
) -> dict:
    """
    Drop-in replacement for ``trigger_solver()`` in controller_v2.py.

    Executes three phases:
      1. USC   – per-node 0/1 knapsack  → initial deployment matrix
      2. USC-TS – tabu search           → globally refined deployment
      3. Round-Robin LB                 → agent-to-replica assignment + frequency

    Port assignment preserves already-allocated ports found in *svcs*
    (same continuity guarantee as the original GA-based solver).

    Parameters
    ----------
    subscriptions : list[dict]
        Active agent subscriptions (same schema as controller_v2.py).
    svcs : list[dict]
        Currently running service instances (for port continuity).
    nodes_status : dict
        ``{node_name: "healthy" | "unhealthy"}``
    specs : dict
        Content of ``serviceSpec_mul.json``.
        uniformCapacity and nodeCapacity are read from
        ``information/usc_ts_config.json`` and merged in automatically.

    Returns
    -------
    dict with keys:
      "target_topology": list of {"nodeName", "serviceType", "hostPort"}
      "allocation":      list of {"agentIP", "agentPort", "targetNode",
                                   "hostPort", "frequency", "serviceType"}
    """
    # Merge USC-TS-specific config (uniformCapacity, nodeCapacity)
    specs = _merge_specs(specs)

    # Sanity checks
    nodes = [n for n, status in nodes_status.items() if status == "healthy"]
    if not nodes or not subscriptions:
        return {"target_topology": [], "allocation": []}

    services = list(specs.get("services", {}).keys())
    if not services:
        return {"target_topology": [], "allocation": []}

    # ── Compute demand (agents per service) ───────────────────────────────
    demand: Dict[str, int] = {}
    for sub in subscriptions:
        for s_entry in sub.get("subscriptions", []):
            s = s_entry["serviceType"]
            demand[s] = demand.get(s, 0) + 1

    # ── Derived quantities ─────────────────────────────────────────────────
    psi = compute_service_value(services, specs, demand)
    n_min = compute_min_replicas(services, specs, demand)
    w = _service_weights(specs)
    R_hat = _get_node_capacity(specs, nodes)

    logger.info(
        "USC-TS input: demand=%s psi=%s n_min=%s nodes=%s R_hat=%s",
        demand, psi, n_min, nodes, R_hat,
    )

    # ── Phase 1: USC – per-node knapsack ──────────────────────────────────
    init_x: Dict[str, list] = {}
    for m in nodes:
        init_x[m] = usc_single_node(services, psi, w, R_hat[m])

    logger.info("USC initial deployment: %s", init_x)

    # ── Phase 2: USC-TS – global tabu search ──────────────────────────────
    best_topology = usc_tabu_search(
        init_x=init_x,
        nodes=nodes,
        services=services,
        psi=psi,
        n_min=n_min,
        R_hat_per_node=R_hat,
        weights=w,
        max_iter=50,
        tabu_size=10,
        alpha=1.0,
        beta=100.0,  # β >> α: covering services takes priority over trimming replicas
    )

    logger.info("USC-TS final deployment: %s", best_topology)

    # ── Phase 3: Round-Robin load balancing ───────────────────────────────
    target_topology, allocation = round_robin_lb(best_topology, subscriptions, specs)

    # ── Port assignment (continuity with existing svcs) ───────────────────
    used_ports: set = set()
    existing_mapping: Dict[Tuple[str, str], int] = {}
    for svc in svcs:
        p = int(svc["hostPort"])
        used_ports.add(p)
        existing_mapping[(svc["nodeName"], svc["serviceType"])] = p

    port_cursor = [31000]

    def _assign_port(node_name: str, service_type: str) -> int:
        key = (node_name, service_type)
        if key in existing_mapping:
            return existing_mapping[key]
        while port_cursor[0] in used_ports:
            port_cursor[0] += 1
        p = port_cursor[0]
        used_ports.add(p)
        existing_mapping[key] = p
        port_cursor[0] += 1
        return p

    for entry in target_topology:
        entry["hostPort"] = _assign_port(entry["nodeName"], entry["serviceType"])

    for alloc in allocation:
        alloc["hostPort"] = _assign_port(alloc["targetNode"], alloc["serviceType"])

    logger.info(
        "USC-TS solver completed: %d pods, %d agent allocations",
        len(target_topology), len(allocation),
    )
    return {"target_topology": target_topology, "allocation": allocation}


# ===========================================================================
# Deployment-only entry point (for EMS + Oracle integration)
# ===========================================================================

def get_usc_ts_deployment(
    nodes: list,
    services: list,
    subscriptions: list,
    specs: dict,
) -> Dict[str, List[str]]:
    """
    Run USC + USC-TS and return only the deployment topology.
    Does NOT perform frequency allocation or port assignment.

    Used by controller_v2.py (SOLVER_MODE='ems') so that the shared
    allocate_x_packed_dict oracle can assign frequencies on top.

    Returns
    -------
    {node_name: sorted list of service names deployed on that node}
    """
    specs = _merge_specs(specs)

    if not nodes or not services:
        return {n: [] for n in nodes}

    demand: Dict[str, int] = {}
    for sub in subscriptions:
        for s_entry in sub.get("subscriptions", []):
            s = s_entry["serviceType"]
            demand[s] = demand.get(s, 0) + 1

    psi = compute_service_value(services, specs, demand)
    n_min = compute_min_replicas(services, specs, demand)
    w = _service_weights(specs)
    R_hat = _get_node_capacity(specs, nodes)

    # Phase 1: USC – per-node knapsack
    init_x: Dict[str, list] = {m: usc_single_node(services, psi, w, R_hat[m]) for m in nodes}
    logger.info("USC initial deployment: %s", init_x)

    # Phase 2: USC-TS – global tabu search
    best_topology = usc_tabu_search(
        init_x=init_x, nodes=nodes, services=services,
        psi=psi, n_min=n_min, R_hat_per_node=R_hat, weights=w,
        max_iter=50, tabu_size=10, alpha=1.0, beta=100.0,
    )
    logger.info("USC-TS deployment: %s", best_topology)
    return best_topology
