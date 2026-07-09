#!/usr/bin/env python3
import subprocess
import sys
import os
import time
import shutil
import json

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laundering")
PRELOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preload")
TRASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trash")
MANIFEST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transferred_manifest.json")
REMOTE_DIR = "/storage/emulated/0/DCIM/Camera"

# keep some free space on device
SAFETY_BUFFER_BYTES = 0.5 * 1024 * 1024 * 1024  # 0.5 GB

# change this with debug coordinates if profile icon tap misses
PROFILE_TAP_X1 = 1005
PROFILE_TAP_Y1 = 133
PROFILE_TAP_X2 = 270
PROFILE_TAP_Y2 = 1190

def run(cmd_list, capture=False):
    """Runs commands safely across Windows and Mac without shell quirks."""
    print(f"> {' '.join(cmd_list)}")
    if capture:
        result = subprocess.run(cmd_list, shell=False, capture_output=True, text=True)
        return result
    else:
        result = subprocess.run(cmd_list, shell=False)
        if result.returncode != 0:
            print("Command failed. Exiting.")
            sys.exit(1)

def push_file(local_path, remote_dir, retries=3):
    filename = os.path.basename(local_path)
    remote_path = f"{remote_dir}/{filename}"
    
    for attempt in range(1, retries + 1):
        print(f"> Pushing {filename} (attempt {attempt})")
        result = subprocess.run(["adb", "push", local_path, remote_path], shell=False)
        if result.returncode == 0:
            print(f"[SUCCESS] Success: {filename}")
            return True
        print(f"[WARNING] Failed: {filename}")
        if attempt < retries:
            print("Retrying in 2 seconds...")
            time.sleep(2)
    print(f"[ERROR] Giving up on {filename}")
    return False

def get_free_space_bytes():
    try:
        result = subprocess.check_output(["adb", "shell", "df", "/storage/emulated/0"], shell=False).decode()
        lines = result.strip().splitlines()
        if len(lines) < 2:
            print("Could not determine free storage.")
            sys.exit(1)
        
        parts = lines[1].split()
        available_kb = int(parts[3])
        return available_kb * 1024
    except Exception as e:
        print(f"Error fetching device storage details: {e}")
        sys.exit(1)

def save_manifest(files):
    with open(MANIFEST_FILE, "w") as f:
        json.dump(files, f, indent=2)

def load_manifest():
    if not os.path.exists(MANIFEST_FILE):
        return []
    with open(MANIFEST_FILE, "r") as f:
        return json.load(f)

def clear_manifest():
    if os.path.exists(MANIFEST_FILE):
        os.remove(MANIFEST_FILE)

def main():
    cleanup_mode = "--cleanup" in sys.argv
    if cleanup_mode:
        print("[ERROR] Manual cleanup mode with prompt has been deprecated for UI automation.")
        sys.exit(1)

    # 1. check adb
    run(["adb", "version"])

    # 2. get local files
    try:
        files = [
            f for f in os.listdir(LOCAL_DIR) 
            if os.path.isfile(os.path.join(LOCAL_DIR, f))
        ]
    except FileNotFoundError:
        print("Local laundering folder not found.")
        sys.exit(1)

    if not files:
        print("No files found in laundering folder.")
        sys.exit(0)

    os.makedirs(PRELOAD_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)

    already_done = set(load_manifest())

    # 3. get total size
    total_bytes = sum(os.path.getsize(os.path.join(LOCAL_DIR, f)) for f in files)
    total_mb = total_bytes / (1024 * 1024)
    if total_mb >= 1024:
        print(f"\nFound {len(files)} files (~{total_mb/1024:.2f} GB)")
    else:
        print(f"\nFound {len(files)} files (~{total_mb:.2f} MB)")

    # 4. get phone free space
    free_bytes = get_free_space_bytes()
    usable_bytes = max(0, free_bytes - SAFETY_BUFFER_BYTES)
    print(f"\nPhone free space: {free_bytes / (1024**3):.2f} GB")
    print(f"Usable transfer space: {usable_bytes / (1024**3):.2f} GB\n")

    files_sorted = sorted(files, key=lambda f: os.path.getsize(os.path.join(LOCAL_DIR, f)))

    print("\nStarting file transfers...\n")
    failed_files = []
    transferred_files = load_manifest()
    preloaded_files = []
    trash_files = []
    used_bytes = 0

    for filename in files_sorted:
        local_path = os.path.join(LOCAL_DIR, filename)

        if filename in already_done:
            print(f"⏩ Skipping already transferred: {filename}")
            if filename not in transferred_files:
                transferred_files.append(filename)
            continue

        file_size = os.path.getsize(local_path)

        if used_bytes + file_size > usable_bytes:
            preload_path = os.path.join(PRELOAD_DIR, filename)
            shutil.move(local_path, preload_path)
            preloaded_files.append(filename)
            print(f"📦 Moved to preload: {filename}")
            continue

        success = push_file(local_path, REMOTE_DIR)
        if success:
            transferred_files.append(filename)
            save_manifest(transferred_files)
            used_bytes += file_size
        else:
            failed_files.append(filename)

    print("\nTransfer complete.")
    if failed_files:
        print("\n[WARNING] Some files failed:")
        for f in failed_files:
            print(f" - {f}")
    else:
        print("\n[SUCCESS] All transfers completed successfully.")

    if preloaded_files:
        print("\n📦 Deferred to preload folder:")
        for f in preloaded_files:
            print(f" - {f}")

    # 5. reboot device
    run(["adb", "reboot"])

    # 6. wait for device
    print("\nWaiting for device...")
    run(["adb", "wait-for-device"])
    time.sleep(7.5)
    print("the program is working dw")
    time.sleep(5.5)

    # wake swipe
    run(["adb", "shell", "input", "swipe", "500", "1500", "500", "500"])
    time.sleep(2.5)

    # 7. open Google Photos
    run(["adb", "shell", "am", "start", "-n", "com.google.android.apps.photos/.home.HomeActivity"])
    time.sleep(4)

    # 8. tap profile icon
    run(["adb", "shell", "input", "tap", str(PROFILE_TAP_X1), str(PROFILE_TAP_Y1)])
    time.sleep(2)
    run(["adb", "shell", "input", "tap", str(PROFILE_TAP_X2), str(PROFILE_TAP_Y2)])
    print("\nGoogle Photos opened.")

    # NEW: Stabilization Wait Time
    # Wait 60 seconds to let Google Photos index the fresh local media before checking text
    print("Waiting 60 seconds for Google Photos to register new files...")
    time.sleep(60)

    # NEW: Automated UI Scan Loop (Replaces manual DELETE prompts)
    print("Beginning automated screen monitoring for backup verification...")
    while True:
        print("Checking Google Photos status...")
        
        # Dump UI XML to a location on the phone, suppress errors, then fetch contents
        run(["adb", "shell", "uiautomator", "dump", "/data/local/tmp/uidump.xml"])
        res = run(["adb", "shell", "cat", "/data/local/tmp/uidump.xml"], capture=True)
        ui_text = res.stdout

        # Target keywords matching your script strings
        targets = ["Backup complete", "Back up complete", "is complete", "All photos backed up", "Your backup is complete"]
        if any(target in ui_text for target in targets):
            print("[SUCCESS] Backup completion text found on screen!")
            break

        print("Still backing up... waiting 5 seconds.")
        time.sleep(5)

    # 9. Automated Purging Process (Runs natively once loop breaks successfully)
    print("\nProceeding to automated file purge...")
    for filename in transferred_files:
        run(["adb", "exec-out", "rm", "-f", "--", f"{REMOTE_DIR}/{filename}"])
        local_path = os.path.join(LOCAL_DIR, filename)
        trash_path = os.path.join(TRASH_DIR, filename)
        
        if os.path.exists(local_path):
            shutil.move(local_path, trash_path)
            trash_files.append(filename)

    clear_manifest()

    # 11. final summary
    transferred_mb = used_bytes / (1024 * 1024)
    if transferred_mb >= 1024:
        print(f"\n[SUCCESS] Freed ~{transferred_mb/1024:.2f} GB")
    else:
        print(f"\n[SUCCESS] Freed ~{transferred_mb:.2f} MB")

    # reboot again for media rescan
    run(["adb", "reboot"])

if __name__ == "__main__":
    main()