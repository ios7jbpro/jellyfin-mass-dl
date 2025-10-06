#!/usr/bin/env python3
import os
import re
import requests
from urllib.parse import urljoin, unquote
from tqdm import tqdm

VERIFY_SSL = True  # Set to False if the server has a self-signed cert

def login(base_url, username, password):
    endpoint = urljoin(base_url, "/Users/AuthenticateByName")
    payload = {"Username": username, "Pw": password}
    headers = {
        "Content-Type": "application/json",
        "X-Emby-Authorization": (
            'MediaBrowser Client="JellyfinDownloader", '
            'Device="WindowsPC", DeviceId="12345", Version="10.9.6"'
        )
    }
    r = requests.post(endpoint, json=payload, headers=headers, verify=VERIFY_SSL)
    if r.status_code != 200:
        print("Login failed. Status code:", r.status_code)
        print("Response:", r.text)
        r.raise_for_status()
    data = r.json()
    token = data.get("AccessToken")
    user = data.get("User") or {}
    user_id = user.get("Id")
    if not token or not user_id:
        raise RuntimeError("Login failed or missing token/user id.")
    return token, user_id

def get_items(base_url, token, user_id, params=None):
    params = params or {}
    headers = {"X-MediaBrowser-Token": token}
    endpoint = urljoin(base_url, f"/Users/{user_id}/Items")
    r = requests.get(endpoint, headers=headers, params=params, verify=VERIFY_SSL)
    r.raise_for_status()
    return r.json()

def get_item(base_url, token, item_id):
    headers = {"X-MediaBrowser-Token": token}
    endpoint = urljoin(base_url, f"/Items/{item_id}")
    r = requests.get(endpoint, headers=headers, verify=VERIFY_SSL)
    r.raise_for_status()
    return r.json()

def extract_filename_from_cd(cd_header, fallback_name):
    # Handles both filename and filename*=UTF-8''MyFile.mp3
    if not cd_header:
        return fallback_name
    # Check for RFC 5987 style
    match = re.search(r"filename\*=(?:UTF-8'')?([^;\r\n]+)", cd_header)
    if match:
        return unquote(match.group(1).strip('"'))
    # Fallback to basic filename=
    match = re.search(r'filename="?([^";\n]+)"?', cd_header)
    if match:
        return match.group(1).strip()
    return fallback_name

def download_item_file(base_url, token, item_id, dest_folder):
    headers = {"X-MediaBrowser-Token": token}
    url = urljoin(base_url, f"/Items/{item_id}/Download")
    with requests.get(url, headers=headers, stream=True, verify=VERIFY_SSL) as r:
        r.raise_for_status()
        item_meta = get_item(base_url, token, item_id)
        fallback_name = item_meta.get("Name") or f"{item_id}"
        cd = r.headers.get("Content-Disposition")
        fname = extract_filename_from_cd(cd, fallback_name)

        # Clean up Windows-illegal characters
        invalid_chars = r'\/:*?"<>|'
        safe_fname = "".join(c for c in fname if c not in invalid_chars).strip()
        os.makedirs(dest_folder, exist_ok=True)
        out_path = os.path.join(dest_folder, safe_fname)

        if os.path.exists(out_path):
            print(f"⚠️ Skipping (already exists): {safe_fname}")
            return out_path

        total = int(r.headers.get("Content-Length") or 0)
        chunk_size = 1024 * 32
        with open(out_path, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=safe_fname) as pbar:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))
    return out_path

def safe_name(s):
    if not s:
        return "Unknown"
    invalid = r'\/:*?"<>|'
    return "".join(c for c in s if c not in invalid).strip()


def fetch_lrclib_lrc(track_name, artist, album, duration):
    import requests, urllib.parse

    params = {
        "track_name": track_name,
        "artist_name": artist,
        "album_name": album,
        "duration": int(duration)
    }
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)

    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("syncedLyrics") or data.get("plainLyrics")
    except Exception as e:
        print(f"   ⚠️ Error fetching lyrics: {e}")
        return None

def save_lrc(lrc_text, folder, track_name):
    safe_fname = "".join(c for c in track_name if c not in r'\/:*?"<>|').strip()
    out_path = os.path.join(folder, safe_fname + ".lrc")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(lrc_text)
    print(f"   ↪️ Saved lyrics: {out_path}")




def download_item_extras(base_url, token, item_meta, folder):
    """
    Download extra files attached to an item, like .lrc, .nfo, covers, etc.
    """
    # MediaSources often contain direct URLs for streams
    for ms in item_meta.get("MediaSources", []):
        for ext in [".lrc", ".txt", ".nfo", ".cue"]:
            name = ms.get("Path", "")
            if name.lower().endswith(ext):
                try:
                    print(f"   ↪️  Downloading extra: {name}")
                    download_item_file(base_url, token, item_meta["Id"], folder)
                except Exception as e:
                    print(f"   ⚠️ Failed to download extra '{name}': {e}")

    # ExtraFiles field (if present)
    for ef in item_meta.get("ExtraFiles", []):
        ef_name = ef.get("Name")
        if ef_name:
            try:
                print(f"   ↪️  Downloading extra file: {ef_name}")
                download_item_file(base_url, token, ef["Id"], folder)
            except Exception as e:
                print(f"   ⚠️ Failed to download extra '{ef_name}': {e}")

    # Covers / images
    for img_type in ["PrimaryImage", "BackdropImage", "Screenshot"]:
        # check if item has an image
        tag = item_meta.get(f"{img_type}Tag")
        if tag:
            img_url = urljoin(base_url, f"/Items/{item_meta['Id']}/Images/{img_type}")
            fname = f"{img_type}.jpg"
            out_path = os.path.join(folder, fname)
            if os.path.exists(out_path):
                continue
            try:
                with requests.get(img_url, headers={"X-MediaBrowser-Token": token}, stream=True, verify=VERIFY_SSL) as r:
                    r.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_content(1024*32):
                            if chunk:
                                f.write(chunk)
                print(f"   ↪️  Downloaded image: {fname}")
            except Exception as e:
                print(f"   ⚠️ Failed to download image '{fname}': {e}")




def main():
    base_url = input("Jellyfin URL (e.g. https://domain.example.com or a local ip): ").strip().rstrip('/')
    username = input("Jellyfin username: ").strip()
    password = input("Jellyfin password: ").strip()
    target_root = input("Local target folder to save files (e.g. ./ripped_out_jellyfin): ").strip()

    print("Logging in...")
    token, user_id = login(base_url, username, password)
    print("✅ Logged in. User ID:", user_id)

    print("Enumerating audio items...")
    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Audio",
        "Fields": "Album,Artists,AlbumArtist,Path,ParentId"
    }
    resp = get_items(base_url, token, user_id, params=params)
    items = resp.get("Items", [])
    print(f"🎵 Found {len(items)} audio items.\n")

    for item in items:
        try:
            artist = (item.get("Artists") or ["Unknown Artist"])[0]
            album = item.get("Album") or "Unknown Album"
            parent_id = item.get("ParentId")
            item_meta = get_item(base_url, token, item["Id"])
            folder = os.path.join(target_root, safe_name(artist), safe_name(album))
            print(f"➡️  Downloading track: {item.get('Name')} -> {folder}")
            audio_path = download_item_file(base_url, token, item["Id"], folder)
            audio_fname = os.path.splitext(os.path.basename(audio_path))[0]
            print("✅ Saved:", audio_path)
            download_item_extras(base_url, token, item_meta, folder)
            duration = item.get("RunTimeTicks", 0) / 10_000_000
            lrc = fetch_lrclib_lrc(item.get("Name"), artist, album, duration)
            if lrc:
                save_lrc(lrc, folder, audio_fname)  # use audio_fname instead of item.get("Name")
            else:
                print(f"   ⚠️ Lyrics not found for {item.get('Name')}")

            # Download associated files (e.g. .lrc, .jpg, etc.)
            if parent_id:
                siblings = get_items(base_url, token, user_id, params={"ParentId": parent_id})
                for s in siblings.get("Items", []):
                    if s.get("Id") == item.get("Id"):
                        continue
                    name = (s.get("Name") or "").lower()
                    if any(name.endswith(ext) for ext in [".lrc", ".txt", ".nfo", ".cue", ".jpg", ".jpeg", ".png"]):
                        try:
                            print(f"   ↪️  Downloading sibling: {s.get('Name')}")
                            download_item_file(base_url, token, s["Id"], folder)
                        except Exception as e:
                            print(f"   ⚠️  Failed to download sibling '{s.get('Name')}': {e}")
        except Exception as e:
            print("❌ Error downloading item", item.get("Name"), e)

    print("\n✅ All done! You can now add the files in", target_root, "to your Jellyfin library.")

if __name__ == "__main__":
    main()
