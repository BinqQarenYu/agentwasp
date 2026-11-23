#!/usr/bin/env python3
"""LAN Sentinel — Lightweight, High-Fidelity Local Network Discovery & Ping Probe.

Part of Agent WASP 'Beyond the PC' capabilities.
Designed for minimal blast radius (D <= 0.05), non-blocking execution, and zero bloat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Default configuration path
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
ROSTER_FILE = CONFIG_DIR / "known_devices.json"

COMMON_PORTS: dict[int, tuple[str, str]] = {
    22: ("SSH", "Secure Shell remote terminal access"),
    80: ("HTTP", "Standard Web Server"),
    443: ("HTTPS", "Encrypted Web Server / TLS"),
    445: ("SMB", "Windows Networking / File Sharing"),
    1434: ("AgentWASP", "Agent WASP Web Cockpit"),
    3000: ("WAHA", "WhatsApp HTTP API Bridge"),
    3389: ("RDP", "Windows Remote Desktop Protocol"),
    8080: ("HTTP-Alt", "Internal Web Service / Dev Server"),
    11434: ("Ollama", "Local LLM Inference Engine"),
}


def resolve_target(target: str) -> tuple[str, str | None]:
    """Resolve an IP address, hostname, or alias from known_devices.json."""
    roster = load_roster()
    for dev in roster.get("devices", []):
        if (
            dev.get("name", "").lower() == target.lower()
            or dev.get("alias", "").lower() == target.lower()
        ):
            return dev.get("ip", target), dev.get("name")
    return target, None


def probe_port(host: str, port: int, timeout_ms: int = 450) -> bool:
    """Perform a fast, bounded TCP handshake to test service availability."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout_ms / 1000.0)
        res = s.connect_ex((host, port))
        s.close()
        return res == 0
    except Exception:
        return False


def probe_services(host: str, ports: list[int] | None = None, timeout_ms: int = 450) -> dict[str, Any]:
    """Probe target host for common developer, AI, and operating system ports."""
    target_ports = ports or list(COMMON_PORTS.keys())
    open_services = []

    with ThreadPoolExecutor(max_workers=min(len(target_ports), 15)) as executor:
        future_map = {
            executor.submit(probe_port, host, p, timeout_ms): p for p in target_ports
        }
        for future in future_map:
            p = future_map[future]
            try:
                if future.result():
                    name, desc = COMMON_PORTS.get(p, ("Custom", "Network Service"))
                    open_services.append({"port": p, "name": name, "description": desc})
            except Exception:
                pass

    open_services.sort(key=lambda x: x["port"])
    return {
        "host": host,
        "open_services": open_services,
        "total_probed": len(target_ports)
    }


def ping_quality(host: str, samples: int = 3) -> dict[str, Any]:
    """Perform multi-sample latency and jitter measurement."""
    latencies = []
    for _ in range(samples):
        res = ping_host_sync(host, timeout_ms=600)
        if res["online"] and res["latency_ms"] is not None:
            latencies.append(res["latency_ms"])
        time.sleep(0.12)

    if not latencies:
        return {"online": False, "packet_loss_pct": 100.0, "samples": samples, "latencies": []}

    avg_lat = round(sum(latencies) / len(latencies), 2)
    min_lat = min(latencies)
    max_lat = max(latencies)
    jitter = round(max_lat - min_lat, 2)
    loss = round(((samples - len(latencies)) / samples) * 100, 1)

    return {
        "online": True,
        "samples": samples,
        "packet_loss_pct": loss,
        "min_ms": min_lat,
        "avg_ms": avg_lat,
        "max_ms": max_lat,
        "jitter_ms": jitter,
        "latencies": latencies
    }


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not ROSTER_FILE.exists():
        initial = {
            "version": "1.0",
            "subnet": "192.168.1.0/24",
            "devices": []
        }
        ROSTER_FILE.write_text(json.dumps(initial, indent=2), encoding="utf-8")


def load_roster() -> dict[str, Any]:
    ensure_config_dir()
    try:
        return json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": "1.0", "devices": []}


def save_roster(data: dict[str, Any]):
    ensure_config_dir()
    ROSTER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ping_host_sync(host: str, timeout_ms: int = 800) -> dict[str, Any]:
    """Execute a single ICMP ping to a target host safely."""
    is_win = platform.system().lower() == "windows"
    cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host] if is_win else ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]

    try:
        t0 = time.perf_counter()
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(2.0, timeout_ms / 1000.0 + 1.0)
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        out = proc.stdout
        out_lower = out.lower()
        is_online = (
            proc.returncode == 0
            and ("ttl=" in out_lower or "time=" in out_lower or "time<" in out_lower)
            and "destination host unreachable" not in out_lower
            and "unreachable" not in out_lower
        )

        latency_ms = None
        if is_online:
            m = re.search(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms", out, re.IGNORECASE)
            if m:
                latency_ms = float(m.group(1))
            else:
                latency_ms = elapsed_ms

        return {
            "host": host,
            "online": is_online,
            "latency_ms": latency_ms,
            "raw_output": out.strip() if not is_online else ""
        }
    except Exception as exc:
        return {"host": host, "online": False, "latency_ms": None, "error": str(exc)}


def get_arp_neighbors() -> list[dict[str, str]]:
    """Retrieve passive ARP cache entries without emitting active network packets."""
    neighbors = []
    is_win = platform.system().lower() == "windows"

    if is_win:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-NetNeighbor -AddressFamily IPv4 | Select-Object IPAddress, LinkLayerAddress, State | ConvertTo-Json"],
                text=True,
                timeout=5
            )
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                ip = item.get("IPAddress", "")
                mac = item.get("LinkLayerAddress", "")
                state = item.get("State", "")
                if ip and not ip.startswith("127.") and not ip.startswith("224.") and not ip.endswith(".255"):
                    neighbors.append({"ip": ip, "mac": mac, "state": str(state)})
        except Exception:
            # Fallback to standard arp -a
            try:
                out = subprocess.check_output(["arp", "-a"], text=True, timeout=5)
                for line in out.splitlines():
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]+)\s+(\w+)", line)
                    if m:
                        neighbors.append({"ip": m.group(1), "mac": m.group(2).replace("-", ":").upper(), "state": m.group(3)})
            except Exception:
                pass
    else:
        try:
            out = subprocess.check_output(["ip", "neigh"], text=True, timeout=5)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] != "fe80:":
                    neighbors.append({"ip": parts[0], "mac": parts[4] if len(parts) > 4 else "UNKNOWN", "state": parts[-1]})
        except Exception:
            pass

    return neighbors


def resolve_hostname(ip: str) -> str:
    """Attempt reverse DNS or NetBIOS name lookup."""
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return name
    except Exception:
        return ""


async def scan_subnet(base_prefix: str = "192.168.1", start: int = 1, end: int = 254, max_workers: int = 60) -> list[dict[str, Any]]:
    """Perform a fast, bounded, asynchronous ping sweep across the target subnet."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    tasks = []

    for i in range(start, end + 1):
        target_ip = f"{base_prefix}.{i}"
        tasks.append(loop.run_in_executor(executor, ping_host_sync, target_ip, 400))

    results = await asyncio.gather(*tasks)
    active = [r for r in results if r["online"]]

    # Enrich active IPs with ARP and Hostname
    arp_entries = {n["ip"]: n for n in get_arp_neighbors()}
    for host in active:
        ip = host["host"]
        host["mac"] = arp_entries.get(ip, {}).get("mac", "UNKNOWN")
        host["hostname"] = resolve_hostname(ip) or "(unresolved)"

    return sorted(active, key=lambda x: [int(p) for p in x["host"].split(".")])


def cli():
    parser = argparse.ArgumentParser(description="LAN Sentinel: Safe Network Device Discovery & Presence Monitoring")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ping command
    p_ping = subparsers.add_parser("ping", help="Ping a specific IP or alias")
    p_ping.add_argument("target", help="IP address, hostname, or alias from known_devices.json")

    # probe command (service discovery)
    p_probe = subparsers.add_parser("probe", help="Safely inspect open developer/AI/network services on target")
    p_probe.add_argument("target", help="IP address, hostname, or alias from known_devices.json")
    p_probe.add_argument("--ports", type=int, nargs="+", help="Specific port numbers to probe")
    p_probe.add_argument("--save", action="store_true", help="Save discovered open services to known_devices.json")
    p_probe.add_argument("--json", action="store_true", help="Output raw JSON")

    # diag command (quality & jitter)
    p_diag = subparsers.add_parser("diag", help="Run multi-sample latency and jitter diagnostics")
    p_diag.add_argument("target", help="IP address, hostname, or alias from known_devices.json")
    p_diag.add_argument("--samples", type=int, default=3, help="Number of ping samples (default: 3)")
    p_diag.add_argument("--json", action="store_true", help="Output raw JSON")

    # scan command
    p_scan = subparsers.add_parser("scan", help="Scan local subnet for active devices")
    p_scan.add_argument("--prefix", default="192.168.1", help="Subnet base (default: 192.168.1)")
    p_scan.add_argument("--start", type=int, default=1, help="Start IP host part (default: 1)")
    p_scan.add_argument("--end", type=int, default=254, help="End IP host part (default: 254)")
    p_scan.add_argument("--json", action="store_true", help="Output raw JSON")

    # arp command (passive)
    p_arp = subparsers.add_parser("arp", help="Inspect passive ARP neighbor cache without sending ping sweeps")
    p_arp.add_argument("--json", action="store_true", help="Output raw JSON")

    # roster command
    p_roster = subparsers.add_parser("roster", help="Manage known devices roster")
    p_roster.add_argument("--add", nargs=3, metavar=("NAME", "IP", "ROLE"), help="Add a known device: name ip role")
    p_roster.add_argument("--list", action="store_true", help="List all known devices and check their live status")

    args = parser.parse_args()

    if args.command == "ping":
        target_ip, alias_name = resolve_target(args.target)
        res = ping_host_sync(target_ip)
        name_str = f" ({alias_name})" if alias_name else ""
        if res["online"]:
            print(f"✅ Host {res['host']}{name_str} is ONLINE | Latency: {res['latency_ms']} ms")
        else:
            print(f"❌ Host {res['host']}{name_str} is OFFLINE / Unreachable")

    elif args.command == "probe":
        target_ip, alias_name = resolve_target(args.target)
        name_str = f" ({alias_name})" if alias_name else ""
        print(f"Probing target {target_ip}{name_str} for safe service availability ...")
        res = probe_services(target_ip, ports=args.ports)

        if args.save:
            roster = load_roster()
            for dev in roster.get("devices", []):
                if dev.get("ip") == target_ip or dev.get("name") == alias_name:
                    dev["open_ports"] = [s["port"] for s in res["open_services"]]
                    dev["services"] = res["open_services"]
                    break
            save_roster(roster)
            print(f"💾 Updated device profile in {ROSTER_FILE.name}")

        if args.json:
            print(json.dumps(res, indent=2))
        else:
            open_count = len(res["open_services"])
            print(f"\nResult: {open_count} open service(s) found on {target_ip}:")
            if open_count > 0:
                print(f"{'Port':<8} {'Service':<14} {'Description'}")
                print("-" * 65)
                for s in res["open_services"]:
                    print(f"{s['port']:<8} {s['name']:<14} {s['description']}")
            else:
                print("No standard developer/AI services are currently listening on the probed ports.")

    elif args.command == "diag":
        target_ip, alias_name = resolve_target(args.target)
        name_str = f" ({alias_name})" if alias_name else ""
        print(f"Running quality diagnostics on {target_ip}{name_str} ({args.samples} samples) ...")
        diag = ping_quality(target_ip, samples=args.samples)
        if args.json:
            print(json.dumps(diag, indent=2))
        else:
            if diag["online"]:
                print(f"Status: ONLINE | Packet Loss: {diag['packet_loss_pct']}%")
                print(f"Latency: min={diag['min_ms']}ms | avg={diag['avg_ms']}ms | max={diag['max_ms']}ms | jitter={diag['jitter_ms']}ms")
                print(f"Samples: {', '.join(f'{l}ms' for l in diag['latencies'])}")
            else:
                print(f"Status: OFFLINE / 100% Packet Loss")

    elif args.command == "arp":
        entries = get_arp_neighbors()
        if args.json:
            print(json.dumps(entries, indent=2))
        else:
            print(f"Discovered {len(entries)} passive ARP neighbor entries:")
            print(f"{'IP Address':<18} {'MAC Address':<20} {'State'}")
            print("-" * 50)
            for e in entries:
                print(f"{e['ip']:<18} {e['mac']:<20} {e.get('state', '')}")

    elif args.command == "scan":
        print(f"Scanning subnet {args.prefix}.{args.start}-{args.end} ...")
        active = asyncio.run(scan_subnet(base_prefix=args.prefix, start=args.start, end=args.end))
        if args.json:
            print(json.dumps(active, indent=2))
        else:
            print(f"\nDiscovered {len(active)} active online devices:")
            print(f"{'IP Address':<18} {'Latency':<12} {'MAC Address':<20} {'Hostname'}")
            print("=" * 70)
            for d in active:
                lat = f"{d['latency_ms']} ms" if d['latency_ms'] is not None else "N/A"
                print(f"{d['host']:<18} {lat:<12} {d.get('mac', 'UNKNOWN'):<20} {d.get('hostname', '')}")

    elif args.command == "roster":
        roster = load_roster()
        if args.add:
            name, ip, role = args.add
            devices = [d for d in roster.get("devices", []) if d.get("ip") != ip and d.get("name") != name]
            devices.append({"name": name, "ip": ip, "role": role, "mac": ""})
            roster["devices"] = devices
            save_roster(roster)
            print(f"✅ Added device '{name}' ({ip}, {role}) to known roster.")
        else:
            devices = roster.get("devices", [])
            print(f"Known Devices ({len(devices)} configured):")
            print(f"{'Name':<20} {'IP Address':<18} {'Role':<15} {'Status'}")
            print("-" * 65)
            for d in devices:
                res = ping_host_sync(d["ip"], timeout_ms=500)
                status_str = f"ONLINE ({res['latency_ms']}ms)" if res["online"] else "OFFLINE"
                print(f"{d['name']:<20} {d['ip']:<18} {d.get('role', 'general'):<15} {status_str}")


if __name__ == "__main__":
    cli()
