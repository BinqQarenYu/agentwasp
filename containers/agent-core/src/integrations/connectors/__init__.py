"""WASP integration connectors.

Each connector is a self-contained module that implements BaseConnector.
Import and register connectors in src/main.py via IntegrationRegistry.register().

Implemented connectors:
    zapier          — Zapier webhook triggers + action invocation
    discord         — Discord webhooks + Bot API
    slack           — Slack incoming webhooks + Web API
    github          — GitHub REST API (issues, PRs, repos, files)
    notion          — Notion REST API (pages, databases, search)
    webhook         — Generic configurable outbound webhook
    home_assistant  — Home Assistant REST API (entities, services)
    mcp             — Model Context Protocol client

Architecture stubs (require separate infrastructure):
    WhatsApp   — Baileys (Node.js service required)
    Signal     — signal-cli (Java binary required)
    Spotify    — OAuth2 flow required
    Sonos      — Local network discovery required
    Philips Hue— Local network + Bridge IP required
    1Password  — CLI binary required
    iOS bridge — iOS companion app required
    macOS bridge — macOS native host app required
"""
