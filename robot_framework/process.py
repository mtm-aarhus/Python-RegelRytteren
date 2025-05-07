"""This module contains the main process of the robot."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import smtplib
import html
from robot_framework import config
from email.message import EmailMessage

import time
import json
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
    data = json.loads(queue_element.data)
     # Assign each field to a named variable

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
        "bikes": data.get("bikes", 0),
        "cars": data.get("cars", 0)
    }
    # 📁 Ensure GraphHopper directory structure
    GRAPHOPPER_DIR.mkdir(parents=True, exist_ok=True)
    
    # 📦 Download GraphHopper JAR if missing
    if not GRAPHOPPER_JAR.exists():
        orchestrator_connection.log_info("Downloading GraphHopper JAR...")
        r = requests.get(GRAPHOPPER_JAR_URL, stream=True)
        with open(GRAPHOPPER_JAR, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        orchestrator_connection.log_info("GraphHopper JAR ready.")

    # 📄 Copy config into GraphHopper directory
    orchestrator_connection.log_info("🔄 Copying config.yml to GraphHopper folder...")
    shutil.copy(CONFIG_SOURCE, CONFIG_DEST)

    # 🌍 Download latest Denmark map if missing or first of the month
    map_url = "https://download.geofabrik.de/europe/denmark-latest.osm.pbf"
    if not MAP_FILE.exists() or datetime.today().day == 1:
        orchestrator_connection.log_info("⬇️ Downloading latest Denmark map...")
        r = requests.get(map_url, stream=True)
        with open(MAP_FILE, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        orchestrator_connection.log_info("Denmark map ready, deleting cache and updating to newest map.")
        if (GRAPHOPPER_DIR / "graph-cache").exists():
            shutil.rmtree(GRAPHOPPER_DIR / "graph-cache")


    # 📦 Download GraphHopper JAR if missing
    if not JAVA_BIN.exists():
        orchestrator_connection.log_info("⬇️ Downloading Adoptium JDK (portable)...")
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
        orchestrator_connection.log_info("JDK ready.")
    
    modtagere = orchestrator_connection.get_constant("RegelRytterenEmails").value
    bccmail = orchestrator_connection.get_constant("jadt").value
    to_address = [email.strip() for email in modtagere.split(",") if email.strip()]
    # 🚚 Fetch locations with metadata
    csv_path = download_henstillinger_csv(USERNAME, PASSWORD, URL)
    
    locations = []
    henstillinger = data.get("henstillinger", False)
    vejmantilladelser = data.get("vejman", False)

    if henstillinger:
        locations += extract_locations_from_csv(csv_path)
    if vejmantilladelser:
        locations += fetch_vejman_locations(token)
    locations = [replace_coord_if_too_close(loc) for loc in locations]
    orchestrator_connection.log_info(f'{len(locations)} stop i alt')
    if locations:
        # 🚀 Launch GraphHopper
        orchestrator_connection.log_info("Launching GraphHopper server...")
        java_cmd = [
            str(JAVA_BIN),
            f"-Ddw.graphhopper.datareader.file={MAP_FILE}",
            "-jar", str(GRAPHOPPER_JAR),
            "server", str(CONFIG_DEST)
        ]
        try:
            gh_process = subprocess.Popen(java_cmd, cwd=GRAPHOPPER_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # 🔄 Wait until GraphHopper is responding
            orchestrator_connection.log_info("⏳ Waiting for GraphHopper to be ready...")
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
                orchestrator_connection.log_info("GraphHopper did not start in time.")
                gh_process.kill()
                exit(1)

            orchestrator_connection.log_info("GraphHopper is running!")

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
            
            # 📬 Send email after solving
            html_body = build_html_email(routes, index_map, locations)
            #SendEmail(to_address = to_address, subject="Dagens ruter",  body=html_body, bcc = bccmail)
            SendEmail(bccmail, subject="Dagens ruter",  body=html_body, bcc = bccmail)
            
            # 🛑 Stop GraphHopper
            orchestrator_connection.log_info("🛑 Stopping GraphHopper server...")
            gh_process.kill()
            orchestrator_connection.log_info("✅ Done.")
        except Exception as e:
            orchestrator_connection.log_info(f"Process failed: {e}")
            gh_process.kill()
            raise(e)
    else:
        #SendEmail(to_address = to_address, subject="Ingen stop i dag",  body="Da der hverken er fundet stop i Vejman eller Mobility Workspace er der ikke nogle ruter i dag", bcc = bccmail)
        SendEmail(bccmail = to_address, subject="Ingen stop i dag",  body="Da der hverken er fundet stop i Vejman eller Mobility Workspace er der ikke nogle ruter i dag", bcc = bccmail)


def build_html_email(routes, index_map, locations):
    html_parts = ['<html><body style="font-family:sans-serif">']
    html_parts.append('<h1>📬 Dagens ruteoversigt</h1>')

    for vehicle, route in routes.items():
        details = get_route_details(route, locations)
        gmaps_link = generate_google_maps_link(route, index_map)

        label = "Cykelrute" if vehicle.startswith("bike") else "Bilrute"
        number = ''.join(filter(str.isdigit, vehicle))
        title = f"{label} {number}"

        html_parts.append(f'<h2><a href="{gmaps_link}" target="_blank">{title}</a></h2>')

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
            if stop["Stop #"] == 0:
                continue  # skip depot
            sag = html.escape(stop.get("løbenummer") or "")
            adresse = html.escape(stop.get("adresse") or "Ikke angivet")
            info = html.escape(stop.get("forseelse") or "")
            nr = stop["Stop #"]
            html_parts.append(f"""
                <tr>
                    <td>{nr}</td>
                    <td>{sag}</td>
                    <td>{adresse}</td>
                    <td>{info}</td>
                </tr>
            """)

        html_parts.append("</tbody></table>")

    html_parts.append("</body></html>")
    return ''.join(html_parts)

def SendEmail(to_address: str | list[str], subject: str, body: str, bcc: str):
    msg = EmailMessage()
    msg['to'] = to_address
    msg['from'] = "RegelRytteren <regelrytteren@aarhus.dk>"
    msg['subject'] = subject
    msg['bcc'] = bcc

    msg.set_content("Please enable HTML to view this message.")
    msg.add_alternative(body, subtype='html')

    # Send message
    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.send_message(msg)