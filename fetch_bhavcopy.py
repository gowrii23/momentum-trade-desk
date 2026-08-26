"""
fetch_bhavcopy.py — downloads the NSE daily Bhavcopy (Cash Market, UDiFF format).

Usage:
    pip install requests
    python fetch_bhavcopy.py                 # fetches today's bhavcopy
    python fetch_bhavcopy.py 26-08-2026       # fetches a specific date (DD-MM-YYYY)
    python fetch_bhavcopy.py --backfill 30    # fetches the last 30 calendar days

Output:
    Saves each day's file as bhavcopy_YYYY-MM-DD.csv in ./bhavcopy_data/
"""

import argparse
import io
import os
import time
import zipfile
from datetime import datetime, timedelta

import requests

OUTPUT_DIR = "bhavcopy_data"
BASE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}


def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get("https://www.nseindia.com", timeout=10)
    session.get("https://www.nseindia.com/all-reports", timeout=10)
    return session


def fetch_one(session, date_obj, out_dir):
    date_str = date_obj.strftime("%Y%m%d")
    url = BASE_URL.format(date=date_str)
    out_path = os.path.join(out_dir, f"bhavcopy_{date_obj.strftime('%Y-%m-%d')}.csv")

    if os.path.exists(out_path):
        print(f"[skip] {out_path} already exists")
        return True

    resp = session.get(url, timeout=20)
    if resp.status_code != 200 or len(resp.content) < 500:
        print(
            f"[miss] {date_obj.strftime('%Y-%m-%d')} — "
            "no file (weekend/holiday, or not yet published)"
        )
        return False

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            with zf.open(csv_name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
        print(f"[ok]   saved {out_path}")
        return True
    except zipfile.BadZipFile:
        print(
            f"[error] {date_obj.strftime('%Y-%m-%d')} — response wasn't a valid zip "
            "(NSE may be rate-limiting or blocking this client)"
        )
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="DD-MM-YYYY (defaults to today)")
    parser.add_argument("--backfill", type=int, help="fetch the last N calendar days")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = get_session()

    if args.backfill:
        today = datetime.now()
        dates = [today - timedelta(days=i) for i in range(args.backfill)]
        dates = [d for d in dates if d.weekday() < 5]
        for day in dates:
            fetch_one(session, day, OUTPUT_DIR)
            time.sleep(1.5)
    else:
        target = datetime.strptime(args.date, "%d-%m-%Y") if args.date else datetime.now()
        fetch_one(session, target, OUTPUT_DIR)


if __name__ == "__main__":
    main()
