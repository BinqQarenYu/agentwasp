"""Sonos connector — local network HTTP control via Sonos HTTP API.

Uses the Sonos local SOAP/HTTP API (no cloud required).
Speaker IP is provided as a secret.

Secrets:
    speaker_ip   — IP address of any Sonos speaker in the household (e.g. 192.168.1.50)

Actions:
    get_state       — Get current playback state, volume, track info          (LOW)
    play            — Resume/start playback                                   (MEDIUM)
    pause           — Pause playback                                          (MEDIUM)
    next_track      — Skip to next track                                      (MEDIUM)
    prev_track      — Go to previous track                                    (MEDIUM)
    set_volume      — Set volume (0-100)                                      (MEDIUM)
    get_volume      — Get current volume                                      (LOW)
    play_uri        — Play a specific URI (e.g. Spotify/radio URL)            (HIGH)
    get_rooms       — List available rooms/players                            (LOW)
    group_rooms     — Group rooms together                                    (HIGH)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_SOAP_TIMEOUT = 10


def _soap_action(service: str, action: str, body: str) -> str:
    return (
        f'<?xml version="1.0"?>'
        f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body>'
        f'</s:Envelope>'
    )


class SonosConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="sonos", version="1.0.0", name="Sonos", category="media",
            description=(
                "Control Sonos smart speakers on your local network. "
                "Requires speaker IP address. No cloud account needed."
            ),
            capabilities=["playback_control", "volume_control", "room_management", "uri_playback"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["speaker_ip"],
            config_schema={},
            rate_limits={
                "get_state":   RateLimit(requests_per_minute=30),
                "play":        RateLimit(requests_per_minute=20),
                "pause":       RateLimit(requests_per_minute=20),
                "next_track":  RateLimit(requests_per_minute=20),
                "prev_track":  RateLimit(requests_per_minute=20),
                "set_volume":  RateLimit(requests_per_minute=20),
                "get_volume":  RateLimit(requests_per_minute=30),
                "play_uri":    RateLimit(requests_per_minute=10),
                "get_rooms":   RateLimit(requests_per_minute=20),
                "group_rooms": RateLimit(requests_per_minute=5),
            },
            actions=[
                ActionSpec(id="get_state", description="Get current playback state, track info, and volume",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[
                        ParamSpec("speaker_ip", "string", "Override speaker IP for this call", required=False),
                    ]),
                ActionSpec(id="play", description="Resume or start playback",
                    risk_level=RiskLevel.HIGH, capability="controlled", params=[
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="pause", description="Pause playback",
                    risk_level=RiskLevel.MEDIUM, capability="controlled", params=[
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="next_track", description="Skip to the next track",
                    risk_level=RiskLevel.MEDIUM, capability="controlled", params=[
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="prev_track", description="Go back to the previous track",
                    risk_level=RiskLevel.MEDIUM, capability="controlled", params=[
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="set_volume", description="Set volume level (0-100)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled", params=[
                        ParamSpec("volume", "integer", "Volume level 0-100", required=True),
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="get_volume", description="Get current volume level",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="play_uri", description="Play a specific URI (Spotify URI, radio stream, etc.)",
                    risk_level=RiskLevel.HIGH, capability="restricted", params=[
                        ParamSpec("uri", "string", "URI to play (e.g. x-sonos-spotify:..., http://stream...)", required=True),
                        ParamSpec("title", "string", "Track/stream title (optional)", required=False),
                        ParamSpec("speaker_ip", "string", "Speaker IP to control", required=False),
                    ]),
                ActionSpec(id="get_rooms", description="List all Sonos rooms/players on the network",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[
                        ParamSpec("speaker_ip", "string", "Any speaker IP to discover from", required=False),
                    ]),
                ActionSpec(id="group_rooms", description="Group multiple rooms together for synchronized playback",
                    risk_level=RiskLevel.HIGH, capability="restricted", params=[
                        ParamSpec("coordinator_ip", "string", "IP of the room to be coordinator (master)", required=True),
                        ParamSpec("member_ips", "string", "Comma-separated IPs of rooms to join the group", required=True),
                    ]),
            ],
            homepage="https://www.sonos.com",
            docs_url="https://developer.sonos.com/reference/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        ip = params.get("speaker_ip") or secrets.get("speaker_ip", "")
        if not ip:
            return self.err("speaker_ip secret is required")

        base = f"http://{ip}:1400"

        try:
            if action == "get_state":    return await self._get_state(base)
            if action == "play":         return await self._transport_cmd(base, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
            if action == "pause":        return await self._transport_cmd(base, "Pause", "<InstanceID>0</InstanceID>")
            if action == "next_track":   return await self._transport_cmd(base, "Next", "<InstanceID>0</InstanceID>")
            if action == "prev_track":   return await self._transport_cmd(base, "Previous", "<InstanceID>0</InstanceID>")
            if action == "set_volume":
                vol = max(0, min(100, int(params.get("volume") or 50)))
                return await self._set_volume(base, vol)
            if action == "get_volume":   return await self._get_volume(base)
            if action == "play_uri":     return await self._play_uri(base, params)
            if action == "get_rooms":    return await self._get_rooms(base)
            if action == "group_rooms":  return await self._group_rooms(params, secrets)
        except Exception as exc:
            logger.error("sonos.execute_error", action=action, error=str(exc))
            return self.err(f"Sonos error: {exc}")

        return self.err(f"Unknown action: {action}")

    async def _soap_post(self, url: str, service: str, action: str, body: str) -> ET.Element:
        envelope = _soap_action(service, action, body)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{service}#{action}"',
        }
        async with httpx.AsyncClient(timeout=_SOAP_TIMEOUT) as client:
            r = await client.post(url, content=envelope.encode("utf-8"), headers=headers)
            r.raise_for_status()
            return ET.fromstring(r.text)

    async def _transport_cmd(self, base: str, cmd: str, body: str) -> dict:
        svc = "urn:schemas-upnp-org:service:AVTransport:1"
        url = f"{base}/MediaRenderer/AVTransport/Control"
        await self._soap_post(url, svc, cmd, body)
        return self.ok({"action": cmd.lower(), "status": "ok"})

    async def _get_state(self, base: str) -> dict:
        svc = "urn:schemas-upnp-org:service:AVTransport:1"
        url = f"{base}/MediaRenderer/AVTransport/Control"
        root  = await self._soap_post(url, svc, "GetTransportInfo", "<InstanceID>0</InstanceID>")
        root2 = await self._soap_post(url, svc, "GetPositionInfo", "<InstanceID>0</InstanceID>")

        def find_text(el: ET.Element, tag: str) -> str:
            found = el.find(f".//{tag}")
            return found.text or "" if found is not None else ""

        state       = find_text(root, "CurrentTransportState")
        track_meta  = find_text(root2, "TrackMetaData")
        track_uri   = find_text(root2, "TrackURI")
        position    = find_text(root2, "RelTime")

        title = artist = album = ""
        if track_meta and "<DIDL-Lite" in track_meta:
            try:
                meta_root = ET.fromstring(track_meta)
                title  = meta_root.findtext(".//{http://purl.org/dc/elements/1.1/}title") or ""
                artist = meta_root.findtext(".//{urn:schemas-rinconnetworks-com:metadata-1-0/}artist") or \
                         meta_root.findtext(".//{http://purl.org/dc/elements/1.1/}creator") or ""
                album  = meta_root.findtext(".//{urn:schemas-upnp-org:metadata-1-0/upnp/}album") or ""
            except Exception:
                pass

        vol_result = await self._get_volume(base)
        volume = vol_result.get("data", {}).get("volume", 0)

        return self.ok({
            "state":     state,
            "track_uri": track_uri,
            "title":     title,
            "artist":    artist,
            "album":     album,
            "position":  position,
            "volume":    volume,
        })

    async def _get_volume(self, base: str) -> dict:
        svc = "urn:schemas-upnp-org:service:RenderingControl:1"
        url = f"{base}/MediaRenderer/RenderingControl/Control"
        root = await self._soap_post(url, svc, "GetVolume",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>")
        vol_el = root.find(".//CurrentVolume")
        volume = int(vol_el.text) if vol_el is not None and vol_el.text else 0
        return self.ok({"volume": volume})

    async def _set_volume(self, base: str, volume: int) -> dict:
        svc = "urn:schemas-upnp-org:service:RenderingControl:1"
        url = f"{base}/MediaRenderer/RenderingControl/Control"
        await self._soap_post(url, svc, "SetVolume",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>")
        return self.ok({"volume": volume, "status": "ok"})

    async def _play_uri(self, base: str, params: dict) -> dict:
        uri   = params.get("uri", "")
        title = params.get("title", "Stream")
        svc   = "urn:schemas-upnp-org:service:AVTransport:1"
        url   = f"{base}/MediaRenderer/AVTransport/Control"
        meta = (
            f'&lt;DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            f'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"&gt;'
            f'&lt;item id="1" parentID="-1" restricted="true"&gt;'
            f'&lt;dc:title&gt;{title}&lt;/dc:title&gt;'
            f'&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
            f'&lt;res&gt;{uri}&lt;/res&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;'
        )
        await self._soap_post(url, svc, "SetAVTransportURI",
            f"<InstanceID>0</InstanceID><CurrentURI>{uri}</CurrentURI><CurrentURIMetaData>{meta}</CurrentURIMetaData>")
        await self._transport_cmd(base, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        return self.ok({"playing": uri, "title": title})

    async def _get_rooms(self, base: str) -> dict:
        async with httpx.AsyncClient(timeout=_SOAP_TIMEOUT) as client:
            r = await client.get(f"{base}/status/topology")
            text = r.text
        rooms = []
        try:
            root = ET.fromstring(text)
            for player in root.findall(".//ZonePlayer"):
                loc = player.get("location", "")
                ip  = loc.split("//")[1].split(":")[0] if "://" in loc else ""
                rooms.append({
                    "name":        player.get("ZoneName", ""),
                    "uuid":        player.get("UUID", ""),
                    "ip":          ip,
                    "coordinator": player.get("coordinator", "") == "true",
                })
        except Exception:
            pass
        return self.ok({"rooms": rooms, "count": len(rooms)})

    async def _group_rooms(self, params: dict, secrets: dict) -> dict:
        coordinator_ip = params.get("coordinator_ip", "")
        member_ips     = [ip.strip() for ip in (params.get("member_ips") or "").split(",") if ip.strip()]
        if not coordinator_ip or not member_ips:
            return self.err("coordinator_ip and member_ips are required")

        rooms_result = await self._get_rooms(f"http://{coordinator_ip}:1400")
        rooms        = rooms_result.get("data", {}).get("rooms", [])
        coordinator_uuid = next(
            (r["uuid"] for r in rooms if r.get("ip") == coordinator_ip), ""
        )
        if not coordinator_uuid:
            return self.err(f"Could not find UUID for coordinator {coordinator_ip}")

        joined = []
        for member_ip in member_ips:
            try:
                svc = "urn:schemas-upnp-org:service:AVTransport:1"
                url = f"http://{member_ip}:1400/MediaRenderer/AVTransport/Control"
                await self._soap_post(url, svc, "SetAVTransportURI",
                    f"<InstanceID>0</InstanceID>"
                    f"<CurrentURI>x-rincon:{coordinator_uuid}</CurrentURI>"
                    f"<CurrentURIMetaData></CurrentURIMetaData>")
                joined.append(member_ip)
            except Exception as exc:
                logger.warning("sonos.group_join_failed", ip=member_ip, error=str(exc))

        return self.ok({"coordinator": coordinator_ip, "joined": joined})
