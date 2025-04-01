"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

import time
import requests
import subprocess
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from fetch_location_data import download_henstillinger_csv, extract_locations_from_csv, fetch_vejman_locations
from optimize_routes import solve_vrp, get_route_details, export_mymaps_csv, generate_google_maps_link, plot_routes, replace_coord_if_too_close

# pylint: disable-next=unused-argument
def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    """Do the primary process of the robot."""
    orchestrator_connection.log_trace("Running process.")
    Credentials = orchestrator_connection.get_credential("Mobility_Workspace")
    token = orchestrator_connection.get_credential("VejmanToken").password
    
    DEBUG_FAST_MATRIX = False

    # 🔧 Config
    USERNAME = Credentials.username
    PASSWORD = Credentials.password
    URL = orchestrator_connection.get_constant("MobilityWorkspaceURL").value
    GRAPHOPPER_DIR = Path("C:/Graphhopper")
    GRAPHOPPER_JAR = GRAPHOPPER_DIR / "graphhopper-web-10.0.jar"
    GRAPHOPPER_JAR_URL = "https://github.com/graphhopper/graphhopper/releases/download/10.0/graphhopper-web-10.0.jar"
    MAP_FILE = GRAPHOPPER_DIR / "denmark-latest.osm.pbf"
    CONFIG_SOURCE = Path("config.yml")
    CONFIG_DEST = GRAPHOPPER_DIR / "config.yml"
    JDK_DIR = GRAPHOPPER_DIR / "jdk"
    JAVA_BIN = JDK_DIR / "bin" / "java.exe"

    vehicles_config = {
        "bikes": 1,
        "cars": 1
    }

    # 📁 Ensure GraphHopper directory structure
    GRAPHOPPER_DIR.mkdir(parents=True, exist_ok=True)
    
    # 📦 Download GraphHopper JAR if missing
    if not GRAPHOPPER_JAR.exists():
        print("⬇️ Downloading GraphHopper JAR...")
        r = requests.get(GRAPHOPPER_JAR_URL, stream=True)
        with open(GRAPHOPPER_JAR, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print("✅ GraphHopper JAR ready.")

    # 📄 Copy config into GraphHopper directory
    print("🔄 Copying config.yml to GraphHopper folder...")
    shutil.copy(CONFIG_SOURCE, CONFIG_DEST)

    # 🌍 Download latest Denmark map if missing or first of the month
    map_url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    if not MAP_FILE.exists() or datetime.today().day == 1:
        print("⬇️ Downloading latest Denmark map...")
        r = requests.get(map_url, stream=True)
        with open(MAP_FILE, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print("✅ Denmark map ready, deleting cache and updating to newest map.")
        shutil.rmtree(GRAPHOPPER_DIR / "graph-cache")


    # 📦 Download GraphHopper JAR if missing
    if not JAVA_BIN.exists():
        print("⬇️ Downloading Adoptium JDK (portable)...")
        jdk_zip_url = "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.10%2B7/OpenJDK17U-jre_x64_windows_hotspot_17.0.10_7.zip"
        jdk_zip_path = GRAPHOPPER_DIR / "jdk.zip"
        r = requests.get(jdk_zip_url, stream=True)
        with open(jdk_zip_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        with zipfile.ZipFile(jdk_zip_path, 'r') as zip_ref:
            extract_temp = GRAPHOPPER_DIR / "jdk_temp"
            extract_temp.mkdir(exist_ok=True)
            zip_ref.extractall(extract_temp)
            subdirs = [d for d in extract_temp.iterdir() if d.is_dir()]
            if subdirs:
                inner_jdk = subdirs[0]
                JDK_DIR.mkdir(exist_ok=True)
                for item in inner_jdk.iterdir():
                    shutil.move(str(item), str(JDK_DIR))
            shutil.rmtree(extract_temp)
        jdk_zip_path.unlink()
        print("✅ JDK ready.")
        
    # 🚀 Launch GraphHopper
    print("🚀 Launching GraphHopper server...")
    java_cmd = [
        str(JAVA_BIN),
        f"-Ddw.graphhopper.datareader.file={MAP_FILE}",
        "-jar", str(GRAPHOPPER_JAR),
        "server", str(CONFIG_DEST)
    ]
    gh_process = subprocess.Popen(java_cmd, cwd=GRAPHOPPER_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 🔄 Wait until GraphHopper is responding
    print("⏳ Waiting for GraphHopper to be ready...")
    ready = False
    for _ in range(600):
        try:
            r = requests.get("http://localhost:8989/")
            if r.status_code == 200:
                ready = True
                break
        except:
            pass
        time.sleep(2)

    if not ready:
        print("❌ GraphHopper did not start in time.")
        gh_process.kill()
        exit(1)

    print("✅ GraphHopper is running!")


    # 🚚 Fetch locations with metadata
    csv_path = download_henstillinger_csv(USERNAME, PASSWORD, URL)
    henstillinger_locations = extract_locations_from_csv(csv_path)
    vejman_locations = fetch_vejman_locations(token)

    locations = henstillinger_locations+vejman_locations
    locations = [replace_coord_if_too_close(loc) for loc in locations]
    print(f'{len(locations)} stop i alt')

    routes, index_map = solve_vrp(locations, vehicles_config, use_cache=DEBUG_FAST_MATRIX)

    for vehicle, route in routes.items():
        details = get_route_details(route, locations)
        gmaps_link = generate_google_maps_link(route, index_map)

        print(f"{vehicle}")
        for stop in details:
            print(f"  Stop {stop['Stop #']}: {stop.get('løbenummer')} {stop.get('adresse', 'Depot')} - {stop.get('forseelse', '')}")
        print(f"🔗 Google Maps: {gmaps_link}")
        # export_mymaps_csv(details, f"mymaps_{vehicle}.csv")

    # plot_routes((routes, index_map, "Route"))

    # 🛑 Stop GraphHopper
    print("🛑 Stopping GraphHopper server...")
    gh_process.kill()
    print("✅ Done.")

