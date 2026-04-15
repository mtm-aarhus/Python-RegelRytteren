"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

import smtplib
import json
import requests
import subprocess
import shutil
import zipfile
import time
from datetime import datetime
from pathlib import Path
from email.message import EmailMessage

from robot_framework import config
from optimize_routes import solve_vrp, get_route_details, generate_google_maps_links


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Do the primary process of the robot."""
    orchestrator_connection.log_trace("Running process.")

    data = json.loads(queue_element.data)
    inspectors = data.get("inspectors", [])
    include_vejman = data.get("vejman", True)
    include_henstillinger = data.get("henstillinger", True)

    if not inspectors:
        orchestrator_connection.log_info("No inspectors selected, skipping.")
        return

    # Build vehicles config from inspectors (order: bikes first, then cars)
    sorted_inspectors = sorted(inspectors, key=lambda i: (0 if i["vehicle"] == "Cykel" else 1))
    vehicles_config = {
        "bikes": sum(1 for i in sorted_inspectors if i["vehicle"] == "Cykel"),
        "cars": sum(1 for i in sorted_inspectors if i["vehicle"] == "Bil"),
    }

    # Email recipients from inspector initials
    to_addresses = [f"{i['initial']}@aarhus.dk" for i in sorted_inspectors]
    bccmail = orchestrator_connection.get_constant("jadt").value

    # Fetch locations from the unified tasks API
    api_cred = orchestrator_connection.get_credential("OpenOrchestratorAPIKey")
    api_url = api_cred.username
    api_key = api_cred.password

    resp = requests.get(
        f"{api_url}tilsyn/tasks",
        headers={"X-API-Key": api_key},
        timeout=60,
    )
    resp.raise_for_status()
    items = resp.json()

    # Filter by type and build location list
    locations = []
    for item in items:
        item_type = item.get("type")
        if item_type == "permission" and not include_vejman:
            continue
        if item_type == "henstilling" and not include_henstillinger:
            continue

        lat = item.get("latitude")
        lon = item.get("longitude")
        if lat is None or lon is None:
            continue

        if item_type == "permission":
            case_ref = item.get("case_number", "")
            info = item.get("rovm_equipment_type", "")
            case_id = item.get("case_id")
            case_url = f"https://vejman.vd.dk/permissions/update.jsp?caseid={case_id}" if case_id else None
        else:
            case_ref = item.get("HenstillingId", "")
            info = item.get("Forseelse", "")
            pezuuid = item.get("PEZUUID")
            case_url = f"https://pez.giantleap.net/cases/view/{pezuuid}/case" if pezuuid else None

        locations.append({
            "coord": (lat, lon),
            "adresse": item.get("full_address", ""),
            "løbenummer": case_ref,
            "forseelse": info,
            "case_url": case_url,
        })

    orchestrator_connection.log_info(f"{len(locations)} stop i alt")

    if not locations:
        send_email(
            to_address=to_addresses,
            subject="Ingen stop i dag",
            body="Da der hverken er fundet stop i Vejman eller henstillinger er der ikke nogle ruter i dag.",
            bcc=bccmail,
        )
        return

    # GraphHopper setup
    GRAPHHOPPER_DIR = Path("C:/Graphhopper")
    GRAPHHOPPER_JAR = GRAPHHOPPER_DIR / "graphhopper-web-11.0.jar"
    GRAPHHOPPER_JAR_URL = "https://github.com/graphhopper/graphhopper/releases/download/11.0/graphhopper-web-11.0.jar"
    MAP_FILE = GRAPHHOPPER_DIR / "denmark-latest.osm.pbf"
    CONFIG_SOURCE = Path("config.yml")
    CONFIG_DEST = GRAPHHOPPER_DIR / "config.yml"
    JDK_DIR = GRAPHHOPPER_DIR / "jdk"
    JAVA_BIN = JDK_DIR / "bin" / "java.exe"

    GRAPHHOPPER_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any stale JARs from previous GraphHopper versions, and wipe
    # the graph-cache if we detect a version change (graph-cache is built
    # by a specific GH version and is NOT forward/backward compatible).
    stale_jars = [
        p for p in GRAPHHOPPER_DIR.glob("graphhopper-web-*.jar")
        if p.name != GRAPHHOPPER_JAR.name
    ]
    if stale_jars:
        for p in stale_jars:
            orchestrator_connection.log_info(f"Removing stale GraphHopper JAR: {p.name}")
            p.unlink()
        cache_dir = GRAPHHOPPER_DIR / "graph-cache"
        if cache_dir.exists():
            orchestrator_connection.log_info("Removing graph-cache built by old GraphHopper version.")
            shutil.rmtree(cache_dir)

    if not GRAPHHOPPER_JAR.exists():
        orchestrator_connection.log_info("Downloading GraphHopper JAR...")
        r = requests.get(GRAPHHOPPER_JAR_URL, stream=True)
        with open(GRAPHHOPPER_JAR, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        # Ensure graph-cache is rebuilt against the freshly downloaded JAR.
        cache_dir = GRAPHHOPPER_DIR / "graph-cache"
        if cache_dir.exists():
            orchestrator_connection.log_info("Removing graph-cache after GraphHopper JAR download.")
            shutil.rmtree(cache_dir)

    shutil.copy(CONFIG_SOURCE, CONFIG_DEST)

    map_url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    if not MAP_FILE.exists() or datetime.today().day == 1:
        orchestrator_connection.log_info("Downloading latest Denmark map...")
        r = requests.get(map_url, stream=True)
        with open(MAP_FILE, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        cache_dir = GRAPHHOPPER_DIR / "graph-cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

    if not JAVA_BIN.exists():
        orchestrator_connection.log_info("Downloading Adoptium JDK...")
        jdk_zip_url = "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.10%2B7/OpenJDK17U-jre_x64_windows_hotspot_17.0.10_7.zip"
        jdk_zip_path = GRAPHHOPPER_DIR / "jdk.zip"
        r = requests.get(jdk_zip_url, stream=True)
        with open(jdk_zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        with zipfile.ZipFile(jdk_zip_path, "r") as zip_ref:
            extract_temp = GRAPHHOPPER_DIR / "jdk_temp"
            extract_temp.mkdir(exist_ok=True)
            zip_ref.extractall(extract_temp)
            subdirs = [d for d in extract_temp.iterdir() if d.is_dir()]
            if subdirs:
                JDK_DIR.mkdir(exist_ok=True)
                for item in subdirs[0].iterdir():
                    shutil.move(str(item), str(JDK_DIR))
            shutil.rmtree(extract_temp)
        jdk_zip_path.unlink()

    # Launch GraphHopper and solve
    orchestrator_connection.log_info("Launching GraphHopper server...")
    java_cmd = [
        str(JAVA_BIN),
        f"-Ddw.graphhopper.datareader.file={MAP_FILE}",
        "-jar", str(GRAPHHOPPER_JAR),
        "server", str(CONFIG_DEST),
    ]

    # Discard GraphHopper's chatty stdout. If you need to debug startup,
    # swap these to open a log file + stderr=subprocess.STDOUT — graph-cache
    # rebuilds (e.g. after a version upgrade) can take 10-20 min for Denmark.
    gh_process = subprocess.Popen(
        java_cmd,
        cwd=GRAPHHOPPER_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        orchestrator_connection.log_info("Waiting for GraphHopper to be ready (first run after upgrade rebuilds the graph-cache, up to 20 min)...")
        ready = False
        # Wait up to 30 min — graph import for Denmark can take a while.
        for iteration in range(900):
            # Abort early if the Java process died.
            if gh_process.poll() is not None:
                orchestrator_connection.log_info(
                    f"GraphHopper exited during startup with code {gh_process.returncode}. "
                    f"Re-enable stdout/stderr piping in process.py to diagnose."
                )
                return
            try:
                if requests.get("http://localhost:8989/", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            # Heartbeat every 30s so the robot log shows progress.
            if iteration > 0 and iteration % 15 == 0:
                orchestrator_connection.log_info(f"Still waiting for GraphHopper... ({iteration * 2}s elapsed)")
            time.sleep(2)

        if not ready:
            orchestrator_connection.log_info("GraphHopper did not start in time.")
            gh_process.kill()
            return

        orchestrator_connection.log_info("GraphHopper is running!")

        routes, index_map = solve_vrp(locations, vehicles_config)

        # Map vehicle labels to inspector names for the email
        vehicle_to_inspector = {}
        bike_idx = 0
        car_idx = 0
        for inspector in sorted_inspectors:
            if inspector["vehicle"] == "Cykel":
                bike_idx += 1
                vehicle_to_inspector[f"bike_{bike_idx}"] = inspector["initial"]
            else:
                car_idx += 1
                vehicle_to_inspector[f"car_{car_idx}"] = inspector["initial"]

        route_data = {}
        for vehicle, route in routes.items():
            details = get_route_details(route, locations)
            vehicle_type = "bike" if vehicle.startswith("bike") else "car"
            gmaps_links = generate_google_maps_links(route, index_map, vehicle_type)
            inspector_initial = vehicle_to_inspector.get(vehicle, vehicle)

            route_data[vehicle] = {
                "route": route,
                "details": details,
                "gmaps_links": gmaps_links,
                "vehicle_type": vehicle_type,
                "inspector": inspector_initial,
            }

        html_body = build_html_email(route_data)
        send_email(to_address=to_addresses, subject="Dagens ruter", body=html_body, bcc=bccmail)

        orchestrator_connection.log_info("Done.")
    except Exception as e:
        orchestrator_connection.log_info(f"Process failed: {e}")
        raise
    finally:
        gh_process.kill()


def build_html_email(route_data):
    html_parts = ['<html><body style="font-family:sans-serif">']
    html_parts.append("<h1>Dagens ruteoversigt</h1>")

    for vehicle, data in route_data.items():
        details = data["details"]
        gmaps_links = data["gmaps_links"]
        inspector = data["inspector"]
        vehicle_label = "Cykel" if data["vehicle_type"] == "bike" else "Bil"
        title = f"{inspector} ({vehicle_label})"

        # Google Maps /dir URLs are capped at 9 waypoints + 1 destination, so
        # long routes are split into sequential chunks. Show one link per chunk.
        if len(gmaps_links) == 1:
            html_parts.append(f'<h2><a href="{gmaps_links[0]}" target="_blank">{title}</a></h2>')
        elif len(gmaps_links) > 1:
            link_parts = " | ".join(
                f'<a href="{url}" target="_blank">Del {i + 1}</a>'
                for i, url in enumerate(gmaps_links)
            )
            html_parts.append(f"<h2>{title} — {link_parts}</h2>")
        else:
            html_parts.append(f"<h2>{title}</h2>")
        html_parts.append("""
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse; margin-bottom:30px">
            <thead>
                <tr>
                    <th>#</th>
                    <th>Sagsnummer</th>
                    <th>Adresse</th>
                    <th>Information</th>
                </tr>
            </thead>
            <tbody>
        """)

        for stop in details:
            case_ref = stop.get("løbenummer", "")
            case_url = stop.get("case_url")
            if case_ref and case_url:
                case_cell = f'<a href="{case_url}" target="_blank">{case_ref}</a>'
            else:
                case_cell = case_ref
            html_parts.append(f"""
                <tr>
                    <td>{stop['Stop #']}</td>
                    <td>{case_cell}</td>
                    <td>{stop.get('adresse', 'Depot')}</td>
                    <td>{stop.get('forseelse', '')}</td>
                </tr>
            """)

        html_parts.append("</tbody></table>")

    html_parts.append("</body></html>")
    return "".join(html_parts)


def send_email(to_address: str | list[str], subject: str, body: str, bcc: str):
    msg = EmailMessage()
    msg["to"] = to_address
    msg["from"] = "RegelRytteren <regelrytteren@aarhus.dk>"
    msg["subject"] = subject
    msg["bcc"] = bcc

    msg.set_content("Please enable HTML to view this message.")
    msg.add_alternative(body, subtype="html")

    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.send_message(msg)
