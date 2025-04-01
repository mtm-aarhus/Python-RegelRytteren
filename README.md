# 🚴‍♂️ RegelRytteren – Route Optimization for Aarhus

**RegelRytteren** fetches real-time data from *Mobility Workspace* and *Vejman*, calculates optimal routes using **GraphHopper** and **Google OR-Tools**, and supports both bikes and cars with strict time, distance, and penalty constraints. All components run locally, including launching your own GraphHopper server.

---

## 📦 Prerequisites

1. **OpenOrchestrator**
2. **Google Chrome**


---

## 🧠 What It Does

1. Downloads a fresh **Henstillinger** CSV via Selenium login
2. Fetches **Vejman** permissions via API
3. Cleans & geocodes coordinates
4. Launches **GraphHopper** locally (auto-downloads JAR, map & JDK if missing)
5. Computes elevation-aware travel time and distance matrices
6. Solves a **Vehicle Routing Problem** with OR-Tools:
   - 🚲 Bikes: Max 20 km
   - 🚗 Cars: Penalized for entering central zone
   - ⏱ 6-hour workday per vehicle
   - 🛑 30 minutes per stop
   - ❌ Optional stop dropping with penalties
7. Outputs:
   - 📍 Route details
   - 🔗 Google Maps navigation link
   - (Optional) exportable CSV for MyMaps
   - (Optional) matplotlib route plot

---

## 🗂 File Overview

| File                  | Description |
|-----------------------|-------------|
| `process.py`          | Main script that controls everything (downloads data, launches GH, solves VRP) |
| `optimize_routes.py`  | Contains routing logic, callback definitions, constraints, matrix generation |
| `fetch_location_data.py` | Extracts coordinates from Mobility Workspace (Henstillinger) and Vejman |
| `config.yml`          | Configuration file for GraphHopper |

---

## ⚙️ Configuration

Routing settings are defined in `optimize_routes.py`:

```python
WORK_HOURS = 6              # per vehicle
STOP_TIME = 30              # minutes per stop
TOTAL_MINUTES = WORK_HOURS * 60
MAX_BIKE_KM = 20            # km
CENTER_PENALTY_MINUTES = 20 # for cars in central zone
CENTER_RADIUS_M = 2000      # central zone radius in meters
```

You can adjust the number of bikes/cars in `process.py`:

```python
vehicles_config = {
    "bikes": 1,
    "cars": 1
}
```

---

## 🧰 First-Time Setup (Auto-Handled)

On first run, `process.py` will:

- 📦 Download GraphHopper JAR
- 🌍 Download the Denmark map (`denmark-latest.osm.pbf`)
- 🧠 Save it to `C:/Graphhopper/`
- ☕ Download a portable JDK (Temurin)

You don’t need to manually configure anything — just run the script!

---

## 🏁 Running the System

```bash
python process.py
```

The script will:

1. Log into Mobility Workspace and fetch the latest data
2. Fetch Vejman data
2. Launch GraphHopper locally
3. Solve routes
4. Output results

---

## 🔗 Output Example

```text
bike_1
  Stop 0: Depot - Start/End
  Stop 1: 123456 Testvej 4 - Henstilling til oppfølging
  ...
🔗 Google Maps: https://www.google.com/maps/dir/...
```

---

## 🛑 Cleanup

The GraphHopper process is terminated at the end of `process.py` automatically.

---

## 🧪 Offline Testing

You can manually set `locations` like this:

```python
locations = [
    {"coord": (56.15, 10.20), "adresse": "Fakevej 1", "løbenummer": "X1", "forseelse": "Test"}
]
```

---

## 💬 Questions?

Built by and for Aarhus Kommune’s **Teknik og Miljø** department. Reach out if you want to expand to other cities, vehicle types, or integrate with Orchestrator.

---

<img width="485" alt="image" src="https://github.com/user-attachments/assets/020203ca-d70f-47c9-aaa5-fa01ea71c109" />

---

# Robot-Framework V3

This repo is meant to be used as a template for robots made for [OpenOrchestrator](https://github.com/itk-dev-rpa/OpenOrchestrator).

## Quick start

1. To use this template simply use this repo as a template (see [Creating a repository from a template](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-repository-from-a-template)).
__Don't__ include all branches.

2. Go to `robot_framework/__main__.py` and choose between the linear framework or queue based framework.

3. Implement all functions in the files:
    * `robot_framework/initialize.py`
    * `robot_framework/reset.py`
    * `robot_framework/process.py`

4. Change `config.py` to your needs.

5. Fill out the dependencies in the `pyproject.toml` file with all packages needed by the robot.

6. Feel free to add more files as needed. Remember that any additional python files must
be located in the folder `robot_framework` or a subfolder of it.

When the robot is run from OpenOrchestrator the `main.py` file is run which results
in the following:
1. The working directory is changed to where `main.py` is located.
2. A virtual environment is automatically setup with the required packages.
3. The framework is called passing on all arguments needed by [OpenOrchestrator](https://github.com/itk-dev-rpa/OpenOrchestrator).

## Requirements
Minimum python version 3.10

## Flow

This framework contains two different flows: A linear and a queue based.
You should only ever use one at a time. You choose which one by going into `robot_framework/__main__.py`
and uncommenting the framework you want. They are both disabled by default and an error will be
raised to remind you if you don't choose.

### Linear Flow

The linear framework is used when a robot is just going from A to Z without fetching jobs from an
OpenOrchestrator queue.
The flow of the linear framework is sketched up in the following illustration:

![Linear Flow diagram](Robot-Framework.svg)

### Queue Flow

The queue framework is used when the robot is doing multiple bite-sized tasks defined in an
OpenOrchestrator queue.
The flow of the queue framework is sketched up in the following illustration:

![Queue Flow diagram](Robot-Queue-Framework.svg)

## Linting and Github Actions

This template is also setup with flake8 and pylint linting in Github Actions.
This workflow will trigger whenever you push your code to Github.
The workflow is defined under `.github/workflows/Linting.yml`.

