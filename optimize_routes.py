import requests
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from math import radians, cos, sin, asin, sqrt, isinf
from functools import partial
import re
import os

GRAPHHOPPER_URL = "http://localhost:8989"
DEPOT = (56.161147, 10.13455)
WORK_HOURS = 5.5
STOP_TIME = 20
TOTAL_MINUTES = int(WORK_HOURS * 60)
MAX_BIKE_KM = 30
DEPOT_INDEX = 0
FIXED_COST = 10
CENTER_COORD = (56.15625426608341, 10.214135214922244)
CENTER_RADIUS_M = 2000
CENTER_PENALTY_MINUTES = 20  # add 20 mins to car if going to central location

def get_travel_data(coord1, coord2, mode):
    params = {
        "point": [f"{coord1[0]},{coord1[1]}", f"{coord2[0]},{coord2[1]}"],
        "profile": mode,
        "locale": "da",
        "calc_points": "false"
    }
    try:
        r = requests.get(f"{GRAPHHOPPER_URL}/route", params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        if "paths" in data:
            duration = data["paths"][0]["time"] / 60000  # minutes
            distance = data["paths"][0]["distance"] / 1000  # km
            return duration, distance
    except:
        return float("inf"), float("inf")

def create_distance_matrix(locations, mode, use_cache=True, cache_folder="matrix_cache"):
    os.makedirs(cache_folder, exist_ok=True)
    cache_file = os.path.join(cache_folder, f"{mode}_matrix_{len(locations)}.npz")

    if use_cache and os.path.exists(cache_file):
        print(f"🧠 Loading cached matrix: {cache_file}")
        data = np.load(cache_file)
        
        # Convert deeply into pure Python floats (double `.tolist()` in case of nested arrays)
        time = [[float(cell) for cell in row] for row in data["time"].tolist()]
        dist = [[float(cell) for cell in row] for row in data["dist"].tolist()]
        
        return time, dist

    print(f"🧪 Generating new matrix for mode={mode}...")
    size = len(locations)
    time_matrix = [[0.0 for _ in range(size)] for _ in range(size)]
    dist_matrix = [[0.0 for _ in range(size)] for _ in range(size)]

    for i in range(size):
        for j in range(size):
            if i != j:
                t, d = get_travel_data(locations[i], locations[j], mode)
                time_matrix[i][j] = float(t)
                dist_matrix[i][j] = float(d)
                
    if use_cache:
        np.savez_compressed(cache_file, time=time_matrix, dist=dist_matrix)
        print(f"💾 Saved cache: {cache_file}")
    return time_matrix, dist_matrix


def generate_matrices(locations, vehicle_types, use_cache=True):
    all_coords = [DEPOT] + [loc["coord"] for loc in locations]
    time_matrices = {}
    dist_matrices = {}

    for vtype in set(vehicle_types):
        time_matrices[vtype], dist_matrices[vtype] = create_distance_matrix(
            all_coords, vtype, use_cache=use_cache
        )

    return all_coords, time_matrices, dist_matrices

def time_callback(from_index, to_index, matrix, vtype, manager, coords):
    from_node = manager.IndexToNode(from_index)
    to_node = manager.IndexToNode(to_index)

    base_time = matrix[from_node][to_node]
    service_time = STOP_TIME if to_node != DEPOT_INDEX else 0
    penalty = (
        CENTER_PENALTY_MINUTES
        if vtype == "car" and haversine(coords[to_node], CENTER_COORD) < CENTER_RADIUS_M
        else 0
    )
    return int(base_time + service_time + penalty)


def distance_callback(from_index, to_index, matrix, manager):
    from_node = manager.IndexToNode(from_index)
    to_node = manager.IndexToNode(to_index)
    return int(matrix[from_node][to_node] * 1000)

def extract_solution(solution, routing, manager, time_dimension, all_coords, num_bikes, vehicle_types):
    routes = {}
    index_map = {i: coord for i, coord in enumerate(all_coords)}

    for vehicle_id in range(len(vehicle_types)):
        start = time_dimension.CumulVar(routing.Start(vehicle_id))
        end = time_dimension.CumulVar(routing.End(vehicle_id))
        duration = solution.Value(end) - solution.Value(start)
        label = f"bike_{vehicle_id + 1}" if vehicle_types[vehicle_id] == "bike" else f"car_{vehicle_id + 1 - num_bikes}"
        print(f"⏱ {label}: {duration} minutes")

    for vehicle_id in range(len(vehicle_types)):
        index = routing.Start(vehicle_id)
        route = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            route.append(node)
            index = solution.Value(routing.NextVar(index))
        route.append(manager.IndexToNode(index))
        label = f"bike_{vehicle_id + 1}" if vehicle_types[vehicle_id] == "bike" else f"car_{vehicle_id + 1 - num_bikes}"
        print(f"[DEBUG] {label} has {len(route) - 2} stops (excluding depot)")
        routes[label] = route

    return routes, index_map

def solve_vrp(locations, vehicles_config, use_cache=True):
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
        print("Generating callbacks for " + vtype)
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

    # Enforce hard limits at both start and end, and ask solver to minimize
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
    if total_stops >= vehicle_count * 5:
        min_stops = 4
    else:
        min_stops = max(1, int(total_stops / vehicle_count) - 2)

    for vehicle_id in range(vehicle_count):
        visit_dim.CumulVar(routing.End(vehicle_id)).SetMin(min_stops)

    # Allow dropping locations with a reasonable penalty
    for idx in range(1, len(all_coords)):
        routing.AddDisjunction([manager.NodeToIndex(idx)], 50000)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(120)

    print("🚀 Solving VRP...")
    solution = routing.SolveWithParameters(search_parameters)

    if not solution:
        print("❌ No solution found.")
        return {}

    print("✅ VRP solved. Extracting routes...")
    print("🎯 Objective value:", solution.ObjectiveValue())

    return extract_solution(solution, routing, manager, time_dimension, all_coords, num_bikes, vehicle_types)

def generate_google_maps_link(route, index_map, vehicle_type="bike", enable_navigation=True):
    """
    Generate a Google Maps link for directions starting from current location.

    Args:
        route (list[int]): Route index list from OR-Tools.
        index_map (dict): Maps index to (lat, lon).
        vehicle_type (str): 'bike' or 'car'
        enable_navigation (bool): Whether to add dir_action=navigate to trigger navigation.

    Returns:
        str: A full Google Maps directions link.
    """
    if len(route) < 2:
        return "No valid route."

    # Convert vehicle type to Google Maps travel mode
    travelmode = "bicycling" if vehicle_type == "bike" else "driving"

    # Exclude depot (assumed index 0)
    coords = [index_map[i] for i in route[1:]]

    # Destination = last point; waypoints = all in between
    destination = coords[-1]
    waypoints = coords[:-1]

    dest_str = f"{destination[0]},{destination[1]}"
    waypoints_str = "|".join(f"{lat},{lon}" for lat, lon in waypoints)

    url = f"https://www.google.com/maps/dir/?api=1&destination={dest_str}"
    if waypoints_str:
        url += f"&waypoints={waypoints_str}"
    url += f"&travelmode={travelmode}"
    if enable_navigation:
        url += "&dir_action=navigate"

    return url


def export_mymaps_csv(route_details, filename):
    rows = []
    for detail in route_details:
        if "coord" in detail:
            rows.append({
                "Name": f"Stop {detail['Stop #']}: {detail['adresse']}",
                "Description": f"L\u00f8benummer: {detail['l\u00f8benummer']}\n{detail['forseelse']}",
                "Latitude": detail['coord'][0],
                "Longitude": detail['coord'][1],
            })
    pd.DataFrame(rows).to_csv(filename, index=False)

def get_route_details(route, full_location_list):
    details = []
    for i, idx in enumerate(route):
        if idx == 0:
            details.append({"Stop #": i, "adresse": "Blixens", "Description": "Start/End"})
        else:
            meta = full_location_list[idx - 1]  # idx -1 since idx=1 maps to location[0]
            details.append({"Stop #": i, **meta})
    return details

def plot_routes(*route_groups):
    plt.figure(figsize=(10, 8))
    
    for routes, index_map, label_prefix in route_groups:
        for vehicle_name, route in routes.items():
            coords = [index_map[i] for i in route if i in index_map]
            lats, lons = zip(*coords)
            plt.plot(lons, lats, '-o', label=f"{label_prefix}: {vehicle_name}")
    
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.title("Optimized Routes")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()


def haversine(coord1, coord2):
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

def clean_address(address: str) -> str:
    address = address.split("-")[0]
    match = re.match(r"([A-Za-zÆØÅæøå .]+)\s+(\d+)", address.strip())
    if match:
        street, number = match.groups()
        return f"{street.strip()} {number.strip()}"
    return None

def geocode_address(address: str) -> tuple | None:
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": f"{address}, Aarhus, Denmark",
        "format": "json",
        "limit": 1,
    }
    headers = {
        "User-Agent": "AarhusRoutePlanner/1.0 (aarhuskommune.dk)"
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as e:
        print(f"Geocoding failed for '{address}': {e}")
    return None

def replace_coord_if_too_close(location: dict, threshold_m=100) -> dict:
    coord = location["coord"]
    distance = haversine(coord, DEPOT)
    if distance > threshold_m:
        return location
    cleaned_address = clean_address(location["adresse"])
    geocode_coord = geocode_address(cleaned_address)
    if cleaned_address:
        new_coord = geocode_coord

    if not new_coord and not cleaned_address:    
        road_length = get_road_length_estimate(geocode_address(location["adresse"]))
        if road_length < 500:    
            print(f"Road length of {location["adresse"]} estimated to be <500 meters, using location")
            new_coord = geocode_address(location["adresse"])
    if new_coord:
        new_distance = haversine(new_coord, DEPOT)
        if new_distance > threshold_m:
            print(f"🔁 Replacing coordinate for {location['adresse']} ({distance:.1f}m → {new_distance:.1f}m)")
            location["coord"] = new_coord
    return location

def get_road_length_estimate(coord: tuple, mode="bike", sample_distance=0.0015) -> float | None:
    """Estimate actual road length near a point using GraphHopper routing."""
    lat, lon = coord
    offsets = [-2, -1, 0, 1, 2]  # 5x5 grid
    snapped_coords = []

    for dx in offsets:
        for dy in offsets:
            if dx == 0 and dy == 0:
                continue
            sample_lat = lat + dx * sample_distance
            sample_lon = lon + dy * sample_distance
            r = requests.get(f"{GRAPHHOPPER_URL}/nearest", params={
                "point": f"{sample_lat},{sample_lon}",
                "profile": mode
            }, timeout=3)
            try:
                r.raise_for_status()
                data = r.json()
                snapped = tuple(data["coordinates"])
                if snapped:
                    snapped_coords.append(snapped)
            except Exception:
                continue

    # Try routing between all snapped points and get max actual road distance
    max_road_distance = 0
    for i in range(len(snapped_coords)):
        for j in range(i + 1, len(snapped_coords)):
            point1 = snapped_coords[i]
            point2 = snapped_coords[j]

            route = requests.get(f"{GRAPHHOPPER_URL}/route", params={
                "point": [f"{point1[1]},{point1[0]}", f"{point2[1]},{point2[0]}"],
                "profile": mode,
                "locale": "da",
                "calc_points": "false"
            }, timeout=5)

            try:
                route.raise_for_status()
                data = route.json()
                dist_km = data["paths"][0]["distance"] / 1000
                max_road_distance = max(max_road_distance, dist_km * 1000)  # meters
            except Exception:
                continue

    return max_road_distance if max_road_distance > 0 else None

