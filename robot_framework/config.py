"""This module contains configuration constants used across the framework"""

# The number of times the robot retries on an error before terminating.
MAX_RETRY_COUNT = 3

# Whether the robot should be marked as failed if MAX_RETRY_COUNT is reached.
FAIL_ROBOT_ON_TOO_MANY_ERRORS = False

# Number of attempts per queue_element (1 is no retry, 2 is 2 total attempts and so on)
QUEUE_ATTEMPTS = 2

# Error screenshot config
SMTP_SERVER = "smtp.adm.aarhuskommune.dk"
SMTP_PORT = 25
SCREENSHOT_SENDER = "regelrytteren@aarhus.dk"

# Constant/Credential names
ERROR_EMAIL = "Error Email"


# Queue specific configs
# ----------------------

# The name of the job queue (if any)
QUEUE_NAME = "RegelRytteren"

# The limit on how many queue elements to process
MAX_TASK_COUNT = 100

# ----------------------
