import os
import requests
import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from math import radians, cos, sin, asin, sqrt
from functools import partial
from concurrent.futures import ThreadPoolExecutor

MATRIX_WORKERS = 32
_session = requests.Session()

GRAPHHOPPER_URL = "http://localhost:8989"
DEPOT = (56.161147, 10.13455)
WORK_HOURS = 5.5
STOP_TIME = 20
TOTAL_MINUTES = int(WORK_HOURS * 60)
MAX_BIKE_KM = 30
DEPOT_INDEX = 0
CENTER_COORD = (56.15625426608341, 10.214135214922244)
CENTER_RADIUS_M = 2000
CENTER_PENALTY_MINUTES = 20

# Large finite sentinel for unreachable arcs. Keeping this a plain Python int
# (not float("inf"), not a numpy scalar) avoids two separate failure modes:
#   1. int(inf) → OverflowError
#   2. numpy scalars fed through SWIG into OR-Tools can be silently
#      misinterpreted (truncated / treated as 0 / cast wrong) without raising,
#      producing wrong-but-plausible routes. The solver never errors; you
#      just get bad answers you can't easily see.
# 1e9 is vastly beyond any realistic route so the solver will always avoid it.
UNREACHABLE = 10 ** 9


def get_travel_data(coord1, coord2, mode):
    """Return (duration_minutes, distance_km) as plain Python floats.
    Failed lookups return a large finite sentinel — never inf, never numpy —
    because both can silently corrupt downstream OR-Tools behavior."""
    params = {
        "point": [f"{coord1[0]},{coord1[1]}", f"{coord2[0]},{coord2[1]}"],
        "profile": mode,
        "locale": "da",
        "calc_points": "false",
    }
    try:
        r = _session.get(f"{GRAPHHOPPER_URL}/route", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "paths" in data:
            duration = float(data["paths"][0]["time"]) / 60000.0  # minutes
            distance = float(data["paths"][0]["distance"]) / 1000.0  # km
            return duration, distance
    except Exception:
        pass
    return float(UNREACHABLE), float(UNREACHABLE)


def create_distance_matrix(locations, mode, use_cache=False, cache_folder="matrix_cache"):
    """Returns two nested lists of pure Python floats — never numpy scalars.
    OR-Tools callbacks are SWIG-wrapped and can silently misread numpy types
    (no exception, just wrong routes), so we strip numpy at the boundary here
    and the callbacks float()-cast again as belt-and-braces."""
    os.makedirs(cache_folder, exist_ok=True)
    cache_file = os.path.join(cache_folder, f"{mode}_matrix_{len(locations)}.npz")

    if use_cache and os.path.exists(cache_file):
        data = np.load(cache_file)
        # .item() forces numpy scalar → native Python float.
        time_m = [[float(cell) for cell in row] for row in data["time"].tolist()]
        dist_m = [[float(cell) for cell in row] for row in data["dist"].tolist()]
        return time_m, dist_m

    size = len(locations)
    time_matrix = [[0.0] * size for _ in range(size)]
    dist_matrix = [[0.0] * size for _ in range(size)]

    # Build list of all off-diagonal (i, j) pairs and fetch in parallel
    pairs = [(i, j) for i in range(size) for j in range(size) if i != j]

    def fetch(pair):
        i, j = pair
        t, d = get_travel_data(locations[i], locations[j], mode)
        return i, j, t, d

    with ThreadPoolExecutor(max_workers=MATRIX_WORKERS) as pool:
        for i, j, t, d in pool.map(fetch, pairs):
            time_matrix[i][j] = float(t)
            dist_matrix[i][j] = float(d)

    if use_cache:
        np.savez_compressed(cache_file, time=time_matrix, dist=dist_matrix)
    return time_matrix, dist_matrix


def time_callback(from_index, to_index, matrix, vtype, manager, coords):
    from_node = manager.IndexToNode(from_index)
    to_node = manager.IndexToNode(to_index)

    # float() cast before arithmetic, int() cast on return: forces the value
    # through native Python types so OR-Tools' SWIG layer can't silently
    # misread a numpy scalar as garbage.
    base_time = float(matrix[from_node][to_node])
    service_time = STOP_TIME if to_node != DEPOT_INDEX else 0
    penalty = (
        CENTER_PENALTY_MINUTES
        if vtype == "car" and haversine(coords[to_node], CENTER_COORD) < CENTER_RADIUS_M
        else 0
    )
    total = base_time + service_time + penalty
    return min(int(total), UNREACHABLE)


def distance_callback(from_index, to_index, matrix, manager):
    from_node = manager.IndexToNode(from_index)
    to_node = manager.IndexToNode(to_index)
    meters = float(matrix[from_node][to_node]) * 1000.0
    return min(int(meters), UNREACHABLE)


def solve_vrp(locations, vehicles_config, use_cache=False):
    all_coords = [DEPOT] + [loc["coord"] for loc in locations]

    num_bikes = vehicles_config.get("bikes", 0)
    num_cars = vehicles_config.get("cars", 0)
    vehicle_count = num_bikes + num_cars
    vehicle_types = ["bike"] * num_bikes + ["car"] * num_cars

    time_matrices = {}
    dist_matrices = {}
    for vtype in set(vehicle_types):
        time_matrices[vtype], dist_matrices[vtype] = create_distance_matrix(all_coords, vtype, use_cache)

    manager = pywrapcp.RoutingIndexManager(len(all_coords), vehicle_count, DEPOT_INDEX)
    routing = pywrapcp.RoutingModel(manager)

    time_callback_indices = []
    dist_callback_indices = []

    for vehicle_id in range(vehicle_count):
        vtype = vehicle_types[vehicle_id]
        time_cb = partial(time_callback, matrix=time_matrices[vtype], vtype=vtype, manager=manager, coords=all_coords)
        dist_cb = partial(distance_callback, matrix=dist_matrices[vtype], manager=manager)

        time_cb_idx = routing.RegisterTransitCallback(time_cb)
        dist_cb_idx = routing.RegisterTransitCallback(dist_cb)

        routing.SetArcCostEvaluatorOfVehicle(time_cb_idx, vehicle_id)
        routing.SetFixedCostOfVehicle(200 if vtype == "bike" else 1000, vehicle_id)

        time_callback_indices.append(time_cb_idx)
        dist_callback_indices.append(dist_cb_idx)

    routing.AddDimensionWithVehicleTransits(time_callback_indices, 0, TOTAL_MINUTES, True, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")
    time_dimension.SetGlobalSpanCostCoefficient(500)

    for vehicle_id in range(vehicle_count):
        start_var = time_dimension.CumulVar(routing.Start(vehicle_id))
        end_var = time_dimension.CumulVar(routing.End(vehicle_id))
        start_var.SetRange(0, TOTAL_MINUTES)
        end_var.SetRange(0, TOTAL_MINUTES)
        routing.AddVariableMinimizedByFinalizer(start_var)
        routing.AddVariableMinimizedByFinalizer(end_var)

    routing.AddDimensionWithVehicleTransits(dist_callback_indices, 0, 1_000_000, True, "Distance")
    distance_dimension = routing.GetDimensionOrDie("Distance")

    for vehicle_id in range(vehicle_count):
        if vehicle_types[vehicle_id] == "bike":
            distance_dimension.CumulVar(routing.End(vehicle_id)).SetMax(MAX_BIKE_KM * 1000)

    def count_callback(from_index, to_index):
        return int(from_index != DEPOT_INDEX)

    count_cb_idx = routing.RegisterTransitCallback(count_callback)
    routing.AddDimension(count_cb_idx, 0, 100, True, "VisitCount")
    visit_dim = routing.GetDimensionOrDie("VisitCount")
    visit_dim.SetGlobalSpanCostCoefficient(500)

    total_stops = len(locations)
    min_stops = 4 if total_stops >= vehicle_count * 5 else max(1, int(total_stops / vehicle_count) - 2)

    for vehicle_id in range(vehicle_count):
        visit_dim.CumulVar(routing.End(vehicle_id)).SetMin(min_stops)

    for idx in range(1, len(all_coords)):
        routing.AddDisjunction([manager.NodeToIndex(idx)], 50000)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(120)

    solution = routing.SolveWithParameters(search_parameters)

    if not solution:
        return {}, {}

    routes = {}
    index_map = {i: coord for i, coord in enumerate(all_coords)}

    for vehicle_id in range(vehicle_count):
        index = routing.Start(vehicle_id)
        route = []
        while not routing.IsEnd(index):
            route.append(manager.IndexToNode(index))
            index = solution.Value(routing.NextVar(index))
        route.append(manager.IndexToNode(index))
        label = f"bike_{vehicle_id + 1}" if vehicle_types[vehicle_id] == "bike" else f"car_{vehicle_id + 1 - num_bikes}"
        routes[label] = route

    return routes, index_map


def get_route_details(route, full_location_list):
    details = []
    for i, idx in enumerate(route):
        if idx == 0:
            details.append({"Stop #": i, "adresse": "Blixens", "Description": "Start/End"})
        else:
            meta = full_location_list[idx - 1]
            details.append({"Stop #": i, **meta})
    return details


def generate_google_maps_links(route, index_map, vehicle_type="bike", enable_navigation=True, max_stops_per_link=10):
    """Build Google Maps directions URL(s) for a route.

    Google Maps' /dir/?api=1 URL format supports max 9 waypoints + 1 destination
    = 10 stops per link. Extra waypoints are silently discarded. For long routes
    we split into sequential chunks — each chunk's last stop is the starting
    origin of the next chunk (so the driver never skips ground).

    Returns a list of URLs (one per chunk).
    """
    if len(route) < 2:
        return []

    travelmode = "bicycling" if vehicle_type == "bike" else "driving"
    coords = [index_map[i] for i in route[1:]]  # skip leading depot; include trailing depot
    if not coords:
        return []

    def _build(chunk_coords):
        destination = chunk_coords[-1]
        waypoints = chunk_coords[:-1]
        dest_str = f"{destination[0]},{destination[1]}"
        url = f"https://www.google.com/maps/dir/?api=1&destination={dest_str}"
        if waypoints:
            url += "&waypoints=" + "|".join(f"{lat},{lon}" for lat, lon in waypoints)
        url += f"&travelmode={travelmode}"
        if enable_navigation:
            url += "&dir_action=navigate"
        return url

    # If it fits in one link, return a single-item list.
    if len(coords) <= max_stops_per_link:
        return [_build(coords)]

    # Otherwise split into overlapping chunks: each chunk ends at a stop that
    # becomes the next chunk's starting reference. We pack up to max_stops_per_link
    # per chunk.
    links = []
    i = 0
    step = max_stops_per_link - 1  # overlap one stop so the routes visually chain
    while i < len(coords):
        chunk = coords[i:i + max_stops_per_link]
        if not chunk:
            break
        links.append(_build(chunk))
        if i + max_stops_per_link >= len(coords):
            break
        i += step
    return links


def haversine(coord1, coord2):
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return R * c
