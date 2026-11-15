"""Opt-in, password-protected read access from explicitly configured LANs."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import secrets
import threading
import time
from pathlib import Path

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class LanAccess:
    def __init__(self, addresses=(), password=""):
        self.interfaces = tuple(ipaddress.IPv4Interface(value) for value in addresses)
        for address in self.interfaces:
            if (not any(address.network.subnet_of(net) for net in PRIVATE_NETWORKS)
                    or address.ip in (address.network.network_address, address.network.broadcast_address)):
                raise ValueError("LAN addresses must be usable RFC1918 IPv4 addresses with subnet prefixes")
        if self.interfaces and (not isinstance(password, str) or len(password) < 12 or not password.isascii()):
            raise ValueError("LAN access requires an ASCII password of at least 12 characters")
        self._credential = ("viewer:" + password).encode("ascii")
        self._failures = {}
        self._lock = threading.Lock()

    @classmethod
    def configure(cls, root: Path, addresses):
        """Persist an explicit owner opt-in; never modify proxy/firewall settings."""
        path = root / "output/control_center/lan-access.local.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        password = previous.get("password") or secrets.token_urlsafe(12)
        cls(addresses, password)  # Validate before creating or changing anything.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"enabled": True, "addresses": addresses, "password": password}, indent=2) + "\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return password

    @classmethod
    def load(cls, root: Path):
        path = root / "output/control_center/lan-access.local.json"
        if not path.is_file():
            return cls()
        config = json.loads(path.read_text(encoding="utf-8"))
        if config.get("enabled") is not True:
            return cls()
        addresses = config.get("addresses")
        if not isinstance(addresses, list) or not addresses:
            raise ValueError("LAN addresses must be a non-empty list")
        return cls(addresses, config.get("password", ""))

    @property
    def enabled(self):
        return bool(self.interfaces)

    def urls(self, port):
        return [f"http://{address.ip}:{port}/" for address in self.interfaces]

    def allows(self, client, host, port):
        try:
            peer = ipaddress.IPv4Address(client)
        except ipaddress.AddressValueError:
            return False
        return any(host == f"{address.ip}:{port}" and peer in address.network for address in self.interfaces)

    def authenticate(self, client, authorization):
        """Return an HTTP status; missing credentials do not count as bad guesses."""
        now = time.monotonic()
        with self._lock:
            self._failures = {ip: entry for ip, entry in self._failures.items() if now - entry[0] < 60}
            started, attempts = self._failures.get(client, (now, 0))
            if attempts >= 8:
                return 429
            if not authorization:
                return 401
            credential = b""
            try:
                scheme, encoded = authorization.split(" ", 1)
                if scheme.lower() == "basic" and len(encoded) <= 512:
                    credential = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                pass
            if secrets.compare_digest(credential, self._credential):
                self._failures.pop(client, None)
                return 200
            if len(self._failures) >= 512 and client not in self._failures:
                return 429
            self._failures[client] = (started, attempts + 1)
            return 401
