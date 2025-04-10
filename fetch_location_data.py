import os
import csv
from pathlib import Path
import time
from datetime import datetime, timedelta
import re

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import requests
from pyproj import Transformer


from optimize_routes import geocode_address, clean_address, get_road_length_estimate

def get_default_download_folder():
    return str(Path.home() / "Downloads")


def download_henstillinger_csv(username: str, password: str, url: str) -> str:
    download_dir = get_default_download_folder()
    
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument('--remote-debugging-pipe')
    options.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True
    })

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 20)

    driver.get(f"{url}/login")

    try:
        wait.until(EC.presence_of_element_located((By.ID, "j_username"))).send_keys(username)
        driver.find_element(By.NAME, "j_password").send_keys(password)
        driver.find_element(By.NAME, "submit").click()
    except Exception:
        pass  # Login might not be required

    driver.get(f"{url}/parking/tab/4.6")

    henstillinger_link = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//a[span[text()='Henstillinger']]")
    ))    
    henstillinger_link.click()

    wait.until(EC.visibility_of_element_located((
    By.XPATH,
    "//div[contains(@class, 'wicket-modal')]//span[@class='w_captionText' and text()='Henstillinger']"
    )))
    
    from_date_input = wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//label[text()='From date']/following::input[@type='text'][1]"
    )))

    to_date_input = wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//label[text()='To date']/following::input[@type='text'][1]"
    )))


    from_date_input.clear()
    from_date_input.send_keys("01-01-20")
    to_date_input.clear()
    to_date_input.send_keys(datetime.now().strftime("%d-%m-%y"))

    initial_files = set(os.listdir(download_dir))

    driver.find_element(By.XPATH, '//input[@type="submit" and @value="OK"]').click()

    # Wait for download to complete
    timeout = 60
    start_time = time.time()
    downloaded_file = None

    while True:
        current_files = set(os.listdir(download_dir))
        new_files = current_files - initial_files
        csv_files = [file for file in new_files if file.lower().endswith(".csv")]
        if csv_files:
            downloaded_file = os.path.join(download_dir, csv_files[0])
            print(f"Download completed: {downloaded_file}")
            break

        if time.time() - start_time > timeout:
            print("Timeout reached while waiting for a download.")
            break

        time.sleep(1)

    driver.quit()

    if not downloaded_file:
        raise FileNotFoundError("No CSV file was downloaded.")

    return downloaded_file


def extract_locations_from_csv(csv_path: str) -> list:
    locations = []
    with open(csv_path, encoding="cp1252") as f:  
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row["Status på sagen"].strip() == "Henstilling til oppfølging":
                try:
                    lat = float(row["Latitude"].replace(",", "."))
                    lon = float(row["Longitude"].replace(",", "."))
                except ValueError:
                    print(f"{row["Løbenummer"]} has no lat/lon, skipping")
                    continue
                locations.append({
                    "løbenummer": row["Løbenummer"],
                    "adresse": f"{row['Gade']} {row['Husnummer']}",
                    "forseelse": row["Navn på forseelse"],
                    "coord": (lat, lon)
                })
    os.remove(csv_path)
    return locations

def extract_coord_from_linestring(linestring: str) -> tuple[float, float] | None:
    """
    Extract the first coordinate from a LINESTRING and convert from EPSG:25832 to WGS84.
    Returns (lat, lon) or None if parsing fails.
    """
    epsg25832_to_wgs84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    match = re.search(r"\(?([\d.]+)\s+([\d.]+)", linestring)
    if match:
        east, north = map(float, match.groups())
        lon, lat = epsg25832_to_wgs84.transform(east, north)
        return lat, lon
    return None

def fetch_vejman_locations(token: str) -> list[dict]:
    locations = []
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    urls = [
        # udløbne
        "https://vejman.vd.dk/permissions/getcases?"
        "pmCaseStates=3"
        "&pmCaseFields=state,type,case_number,authority_reference_number,"
        "webgtno,start_date,end_date,applicant_folder_number,connected_case,"
        "street_name,building_to,applicant,rovm_equipment_type,initials"
        "&pmCaseWorker=all"
        "&pmCaseTypes='rovm','gt'"
        "&pmCaseVariant=all"
        "&pmCaseTags=ignorerTags"
        "&pmCaseShowAttachments=false"
        f"&endDateFrom={yesterday_str}"
        f"&endDateTo={today_str}"
        f"&token={token}",
        
        # færdigmeldte
        "https://vejman.vd.dk/permissions/getcases?"
        "pmCaseStates=8"
        "&pmCaseFields=state,type,case_number,authority_reference_number,"
        "webgtno,start_date,end_date,applicant_folder_number,connected_case,"
        "street_name,building_to,applicant,rovm_equipment_type,initials"
        "&pmCaseWorker=all"
        "&pmCaseTypes='rovm','gt'"
        "&pmCaseVariant=all"
        "&pmCaseTags=ignorerTags"
        "&pmCaseShowAttachments=false"
        f"&endDateFrom={today_str}"
        f"&endDateTo={today_str}"
        f"&token={token}",
        
        # nye tilladelser
        "https://vejman.vd.dk/permissions/getcases?"
        "pmCaseStates=3,6,8,12"
        "&pmCaseFields=state,type,case_number,authority_reference_number,"
        "webgtno,start_date,end_date,applicant_folder_number,connected_case,"
        "street_name,building_to,applicant,rovm_equipment_type,initials"
        "&pmCaseWorker=all"
        "&pmCaseTypes='rovm','gt'"
        "&pmCaseVariant=all"
        "&pmCaseTags=ignorerTags"
        "&pmCaseShowAttachments=false"
        f"&startDateFrom={today_str}"
        f"&startDateTo={today_str}"
        f"&token={token}"
    ]

    headers = ["Udløbet tilladelse", "Færdigmeldt tilladelse", "Ny tilladelse"]
    initials_filter = ["MAMASA", "LERV"]

    for idx, url in enumerate(urls):
        r = requests.get(url)
        r.raise_for_status()
        cases = r.json().get("cases", [])

        filtered = [case for case in cases if case.get("initials") in initials_filter]
        print(f"Found {len(filtered)} {headers[idx]}")
        updated_cases = []
        for case in filtered:
            case_id = case.get("case_id")
            if not case_id:
                continue
            detail_r = requests.get(f"https://vejman.vd.dk/permissions/getcase?caseid={case_id}&token={token}")
            detail_r.raise_for_status()
            case_detail = detail_r.json().get("data", {})
            case["start_date"] = case_detail.get("start_date", case.get("start_date"))
            case["end_date"] = case_detail.get("end_date", case.get("end_date"))
            updated_cases.append(case)

        # Udløbne filtering
        if headers[idx] == "Udløbet tilladelse":
            def valid_end_time(c):
                try:
                    end = datetime.strptime(c.get("end_date", ""), "%d-%m-%Y %H:%M:%S")
                    return (
                        (end.date() == yesterday.date() and end.hour >= 8) or
                        (end.date() == now.date() and end.hour < 8 and not (end.hour == 0 and end.minute == 0))
                    )
                except:
                    return False
            updated_cases = [c for c in updated_cases if valid_end_time(c)]

        for case in updated_cases:
            address = f'{case.get("street_name", "").strip()} {case.get("building_to", "")}'
            løbenummer = case.get("case_number", "")
            applicant = case.get("applicant", "")
            forseelse = f"{headers[idx]} - {case.get('rovm_equipment_type', '')} - {case.get('connected_case', '')} - {applicant}".strip(" -")
            coord = None

            if "COORD" in case and isinstance(case["COORD"].get("value"), str):
                new_address = address
                coord = extract_coord_from_linestring(case["COORD"]["value"])

            if not coord:
                print(f"Ingen koordinater på tilladelse {løbenummer}, henter koordinater for {address} i stedet")
                new_address = clean_address(address)
                if not new_address:
                    print(f"Kunne ikke finde lokation på {new_address}")
                    continue
                    print("Ser om adressen kan bruges ift. vejlængde")
                    geocode = geocode_address(address)
                    if geocode:
                        road_length = get_road_length_estimate(geocode)
                    else:
                        print("ugyldig addresse")
                        continue
                    if road_length < 1000:    
                        print(f"Road length of {address} estimated to be {road_length} meters, using location")
                        coord = geocode
                    else:
                        print(f"Road length of {address} estimated to be {road_length} meters, skipping location")
                        continue    
                    
                    print(f"Ugyldig adresse")
                else:
                    coord = geocode_address(address)
                
            if not coord and new_address:
                print(f"Intet koordinat eller gyldig adresse for tilladelse {løbenummer}, springer over")
                continue

            
            if coord:
                locations.append({
                    "løbenummer": løbenummer,
                    "adresse": new_address,
                    "forseelse": forseelse,
                    "coord": coord
                })
            

    return locations

