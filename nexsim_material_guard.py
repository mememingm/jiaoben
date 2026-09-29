"""Durable per-card claim shared by all web material retrieval workflows."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from nexsim_platform_activation import exclusive_write


def marker_path(output_dir, base_url, org_id, inventory_id):
    host = urlsplit(base_url).hostname.lower()
    key = f"https://{host}|{org_id}|{inventory_id}"
    return Path(output_dir).resolve() / "installation-attempts" / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def claim(output_dir, client, inventory_id, batch_id):
    path = marker_path(output_dir, client.base, client.org_id, inventory_id)
    exclusive_write(path, json.dumps({"batch_id": batch_id, "state": "attempted"}).encode())
    return path
