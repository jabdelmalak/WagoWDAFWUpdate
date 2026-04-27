import requests
from pathlib import Path
from requests.auth import HTTPBasicAuth
from tqdm import tqdm
import uuid
import time

# ============================================================
# Configuration
# ============================================================

USERNAME = "admin"
PASSWORD = "wago"
IP_ADDRESS = "192.168.1.35"

# ✅ RAUCB FIRMWARE FILE
UPLOAD_FILE = Path(
    "/home/joe/Desktop/PFC-300-Linux_update_V040809_30_rb45af5c7a3.raucb"
    #"/home/joe/Desktop/WP400-Linux_update_V040809_30_rb45af5c7a3.raucb"
    #"/home/joe/Desktop/Banner_Engineering-K30P-20200601-IODD1.1.xml"
)
UPLOAD_FILENAME = UPLOAD_FILE.name
FILE_SIZE = UPLOAD_FILE.stat().st_size

CHUNK_SIZE = 256 * 1024  # 256 KB (firmware-safe)

if CHUNK_SIZE > 512 * 1024:
    raise ValueError("Chunk size exceeds firmware-safe limit (512 KB)")

JSON_HEADERS = {
    "Content-Type": "application/vnd.api+json"
}

# ============================================================
# Session setup
# ============================================================

requests.packages.urllib3.disable_warnings()

session = requests.Session()
session.auth = HTTPBasicAuth(USERNAME, PASSWORD)
session.verify = False

# ============================================================
# Helper: Request fresh upload ID
# ============================================================

def request_upload_id():
    url = (
        f"https://{IP_ADDRESS}/wda/methods/"
        "0-0-firmwareupdate-getuploadids/runs/"
    )

    payload = {
        "data": {
            "id": "0-0-firmwareupdate-getuploadids",
            "type": "runs",
            "attributes": {
                "inArgs": {
                    "FileNames": {"value": [UPLOAD_FILENAME]}
                }
            }
        }
    }

    resp = session.post(url, json=payload, headers=JSON_HEADERS)
    resp.raise_for_status()

    file_id = resp.json()["data"]["attributes"]["outArgs"]["UploadFiles"]["value"][0]
    print(f"✔ Upload ID received: {file_id}")
    return file_id

# ============================================================
# Helper: Multipart PATCH chunk upload
# ============================================================

def patch_chunk(upload_url, data: bytes, start: int, total: int):
    end = start + len(data) - 1
    boundary = f"wago-{uuid.uuid4().hex}"

    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Range: bytes {start}-{end}/{total}\r\n\r\n"
    ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")

    headers = {
        "Content-Type": f"multipart/byteranges; boundary={boundary}",
        "Content-Length": str(len(body)),  # ✅ REQUIRED FOR RAUC
        "Connection": "close"
    }

    resp = session.patch(
        upload_url,
        headers=headers,
        data=body,
        timeout=(10, None),
    )

    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print("❌ PATCH failed – RAUC firmware rejected chunk.")
        print("❌ Upload ID is now invalid.")
        raise
# ============================================================
# Step 1–3: Upload firmware via multipart PATCH
# ============================================================

def perform_upload():
    file_id = request_upload_id()
    upload_url = f"https://{IP_ADDRESS}/files/{file_id}"

    print(
        f"Uploading {UPLOAD_FILENAME} "
        f"({FILE_SIZE:,} bytes) via multipart PATCH..."
    )

    offset = 0
    expected_offset = 0

    with UPLOAD_FILE.open("rb") as f, tqdm(
        total=FILE_SIZE,
        unit="B",
        unit_scale=True,
        desc="Uploading firmware",
    ) as progress:

        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break

            if offset != expected_offset:
                raise RuntimeError(
                    f"Out-of-order chunk: expected {expected_offset}, got {offset}"
                )

            patch_chunk(upload_url, chunk, offset, FILE_SIZE)

            offset += len(chunk)
            expected_offset = offset
            progress.update(len(chunk))

    print("✔ Upload completed")

    # Verify upload size
    resp = session.head(upload_url)
    resp.raise_for_status()

    remote_size = int(resp.headers.get("Content-Length", -1))
    print(f"Controller reports size: {remote_size}")

    if remote_size != FILE_SIZE:
        raise RuntimeError("❌ File size mismatch after upload")

    print("✔ Upload verified")
    return file_id

# ============================================================
# Step 4: Activate firmware
# ============================================================

def activate_firmware():
    print("Activating firmware...")

    url = (
        f"https://{IP_ADDRESS}/wda/methods/"
        "0-0-firmwareupdate-activate/runs/"
    )

    payload = {
        "data": {
            "id": "0-0-firmwareupdate-activate",
            "type": "runs",
            "attributes": {
                "inArgs": {
                    "KeepCustomerApplication": {"value": True}
                }
            }
        }
    }

    resp = session.post(url, json=payload, headers=JSON_HEADERS)
    resp.raise_for_status()

    print("✔ Firmware activated")

# ============================================================
# Step 5: Start firmware update
# ============================================================

def start_firmware_update(file_id):
    print("Starting firmware update...")

    url = (
        f"https://{IP_ADDRESS}/wda/methods/"
        "0-0-firmwareupdate-start/runs/"
    )

    payload = {
        "data": {
            "id": "0-0-firmwareupdate-start",
            "type": "runs",
            "attributes": {
                "inArgs": {
                    "UploadFiles": {"value": [file_id]}
                }
            }
        }
    }

    resp = session.post(url, json=payload, headers=JSON_HEADERS)
    resp.raise_for_status()

    print("✔ Firmware update started")

# ============================================================
# Step 6: Wait for reboot + poll device
# ============================================================

def wait_for_device():
    print("Waiting 120 seconds before polling device...")
    time.sleep(120)

    print("Polling device availability...")
    while True:
        try:
            session.get(
                f"https://{IP_ADDRESS}/wda",
                timeout=5
            )
            print("✔ Device is reachable")
            return
        except requests.RequestException:
            print("Device not reachable yet – retrying in 20 seconds...")
            time.sleep(20)

# ============================================================
# Step 7: Read firmware update status
# ============================================================

def read_fw_update_status():
    print("Reading firmware update status...")

    resp = session.get(
        f"https://{IP_ADDRESS}/wda/parameters/0-0-firmwareupdate-status",
        timeout=5
    )
    resp.raise_for_status()

    status = resp.json()["data"]["attributes"]["value"]
    print(f"Firmware update status: {status}")


# ============================================================
# Step 8: FW Update Finish 
# ============================================================

def finish_fw_update():
    print("Finishing firmware update...")

    url = (
        f"https://{IP_ADDRESS}/wda/methods/"
        "0-0-firmwareupdate-finish/runs/"
    )

    payload = {
        "data": {
            "id": "0-0-firmwareupdate-finish",
            "type": "runs",
            "attributes": {
            "inArgs":{"KeepCustomerApplication":{"value":True},
                #"CustomKeyValuePairs": []
                }
            }
            
        }
    }

    resp = session.post(url, json=payload, headers=JSON_HEADERS)
    resp.raise_for_status()

    print(f"Finishing Firmware Update")

# ============================================================
# Step 8: FW Update Finish 
# ============================================================

def clear_fw_update():
    print("Finishing firmware update...")

    url = (
        f"https://{IP_ADDRESS}/wda/methods/"
        "0-0-firmwareupdate-clear/runs/"
    )

    payload = {
        "data": {
            "id": "0-0-firmwareupdate-clear",
            "type": "runs",
            "attributes": {
            "inArgs":{"KeepCustomerApplication":{"value":True},
                #"CustomKeyValuePairs": []
                }
            }
            
        }
    }

    resp = session.post(url, json=payload, headers=JSON_HEADERS)
    resp.raise_for_status()

    print(f"Clearing Firmware Update")
    
# ============================================================
# Main execution
# ============================================================

if __name__ == "__main__":
    try:
        file_id = perform_upload()
        time.sleep(5)
        activate_firmware()
        time.sleep(30)
        start_firmware_update(file_id)
        wait_for_device()
        read_fw_update_status()
        time.sleep(5)
        finish_fw_update()
        time.sleep(5)
        clear_fw_update()


    except Exception as e:
        print("❌ Firmware update failed:", e)
        raise
