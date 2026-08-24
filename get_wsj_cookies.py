#!/usr/bin/env python3
"""Decrypt Chrome cookies for wsj.com - runs in GUI context"""
import json, os, sqlite3, shutil, tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import subprocess

HOME = Path.home()
COOKIES_DB = HOME / "Library/Application Support/Google/Chrome/Default/Cookies"

def get_chrome_key():
    """Get Chrome key via security command (runs in GUI context)"""
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip()

def decrypt_cookies():
    key_material = get_chrome_key()
    if not key_material:
        print("ERROR: Could not get Chrome key")
        return []
    
    # Derive AES key
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003)
    aes_key = kdf.derive(key_material.encode("utf-8"))
    
    # Copy Cookies DB
    tmp_db = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(str(COOKIES_DB), tmp_db)
    except:
        subprocess.run(["cp", str(COOKIES_DB), tmp_db], capture_output=True)
    
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute(
        "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly "
        "FROM cookies WHERE host_key LIKE '%wsj.com'"
    ).fetchall()
    
    decoded = []
    for row in rows:
        enc_val = row["encrypted_value"]
        if not enc_val or enc_val[:3] != b"v10":
            continue
        try:
            nonce = enc_val[3:15]
            ct = enc_val[15:]
            value = AESGCM(aes_key).decrypt(nonce, ct, None).decode("utf-8")
            if value:
                decoded.append({
                    "name": row["name"],
                    "value": value,
                    "domain": row["host_key"],
                    "path": row["path"] or "/",
                    "secure": bool(row["is_secure"]),
                    "httpOnly": bool(row["is_httponly"]),
                    "sameSite": "Lax",
                })
        except:
            pass
    
    os.unlink(tmp_db)
    print(f"Decrypted {len(decoded)} cookies for wsj.com")
    return decoded

cookies = decrypt_cookies()
# Save to temp file for the main script to read
with open("/tmp/wsj_cookies.json", "w") as f:
    json.dump(cookies, f, ensure_ascii=False)
print(f"Saved to /tmp/wsj_cookies.json: {len(cookies)} cookies")