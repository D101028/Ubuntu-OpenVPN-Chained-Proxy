import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--first-check-url", type=str)
parser.add_argument("--first-check-available-status", type=str)
parser.add_argument("--check-url", type=str)
parser.add_argument("--check-available-status", type=str)
parser.add_argument("--check-interval", type=str)
parser.add_argument("--ping-host", type=str)
parser.add_argument("--ping-interval", type=str)
parser.add_argument("--auto-refresh-interval", type=str)

args = parser.parse_args()

class Config:
    FIRST_CHECK_URL: str = args.first_check_url
    FIRST_CHECK_AVAILABLE_STATUS: str = args.first_check_available_status
    CHECK_URL: str = args.check_url
    CHECK_AVAILABLE_STATUS: str = args.check_available_status
    CHECK_INTERVAL: str = args.check_interval
    PING_HOST: str = args.ping_host
    PING_INTERVAL: str = args.ping_interval
    AUTO_REFRESH_INTERVAL: str = args.auto_refresh_interval



