"""Canonical capability names for the WASP integration platform.

Used as documentation and optional lint for new connectors. All ActionSpec.capability
values SHOULD use one of these canonical names (or a namespaced variant: "category.name").
"""

CAPABILITIES: dict[str, str] = {
    # ── Messaging ──────────────────────────────────────────────────────────
    "send_message":        "Send text or media to a channel or user",
    "read_messages":       "Read or list incoming messages from a channel",
    "send_reaction":       "Add an emoji reaction to a message",
    "delete_message":      "Delete a sent message",
    "edit_message":        "Edit the content of a sent message",
    "list_channels":       "List available channels or conversations",
    "create_channel":      "Create a new channel or conversation",
    "send_dm":             "Send a direct/private message to a user",
    "broadcast":           "Send a message to multiple recipients at once",

    # ── Media ──────────────────────────────────────────────────────────────
    "generate_image":      "Generate images from text prompts via an AI model",
    "search_gif":          "Search for animated GIFs by keyword",
    "send_image":          "Send or attach an image to a message",
    "send_audio":          "Send or attach an audio file",
    "send_file":           "Send or attach a generic file",
    "recognize_audio":     "Identify a song or audio sample",

    # ── Web & Browser ──────────────────────────────────────────────────────
    "browse_web":          "Navigate to a web URL and extract text content",
    "click_element":       "Click a page element by CSS selector",
    "fill_form":           "Fill and submit a web form",
    "take_screenshot":     "Capture a screenshot of a web page or screen",
    "extract_content":     "Extract structured content from a web page",

    # ── Productivity ───────────────────────────────────────────────────────
    "create_task":         "Create a new task or card in a project manager",
    "update_task":         "Update an existing task or card",
    "list_tasks":          "List tasks, cards, or items in a workspace",
    "create_note":         "Create a new note or document",
    "read_note":           "Read the content of a note or document",
    "search_notes":        "Search notes or documents by keyword",
    "manage_calendar":     "Read or write calendar events",

    # ── Developer ──────────────────────────────────────────────────────────
    "create_issue":        "Create an issue or ticket in a tracker",
    "update_issue":        "Update an existing issue or ticket",
    "list_repos":          "List repositories or projects",
    "trigger_workflow":    "Trigger a CI/CD or automation workflow",

    # ── Smart Home ─────────────────────────────────────────────────────────
    "control_device":      "Control a smart home device (lights, switches)",
    "read_sensor":         "Read state or sensor data from a device",
    "set_scene":           "Activate a predefined smart home scene",
    "control_playback":    "Control media playback (play, pause, skip)",
    "set_volume":          "Set playback volume on a device or speaker",

    # ── Security / Vault ───────────────────────────────────────────────────
    "read_vault_item":     "Look up an item in a password manager (metadata only, no secret values)",
    "list_vault_items":    "List vault entries by name, tag, or category (no secret values)",
    "get_item_reference":  "Return an op:// or vault:// URI reference (never the actual value)",

    # ── Platform Bridges ───────────────────────────────────────────────────
    "platform_screenshot": "Capture a screenshot on a local platform (macOS/Windows/Linux)",
    "platform_imessage":   "Send or read iMessage via a local platform bridge",
    "platform_clipboard":  "Read or write the system clipboard",
    "platform_notes":      "Create or read notes in a native notes application",
    "platform_reminders":  "Create or list native reminders",
    "platform_open_url":   "Open a URL in the system's default browser",
    "platform_shortcut":   "Run an allowed automation shortcut or script",
    "platform_info":       "Read system information (CPU, memory, battery, OS version)",

    # ── Email ──────────────────────────────────────────────────────────────
    "send_email":          "Compose and send an email message",
    "read_email":          "Read or list email messages",
    "search_email":        "Search inbox by keyword, sender, or date",
    "manage_folder":       "Move, label, or organise email messages",

    # ── AI Model ───────────────────────────────────────────────────────────
    "generate_text":       "Generate text using a remote AI language model",
    "list_models":         "List models available from an AI provider",
    "provider_health":     "Check the health and latency of an AI provider",

    # ── Social & Decentralised ─────────────────────────────────────────────
    "publish_post":        "Publish a post or note to a social network",
    "read_feed":           "Read a social feed or timeline",
    "get_profile":         "Retrieve a user's public profile",
    "search_content":      "Search public content on a platform",

    # ── Sleep & Wellness ──────────────────────────────────────────────────
    "read_sleep_data":     "Read sleep tracking data from a wearable or device",
    "set_temperature":     "Adjust temperature setting on a sleep or climate device",
    "set_alarm":           "Create or update an alarm on a device",

    # ── Generic Automation ────────────────────────────────────────────────
    "trigger_webhook":     "Send an HTTP webhook to an external service",
    "invoke_automation":   "Invoke an external automation or integration pipeline",
    "get_status":          "Retrieve the current status of a service or device",
}

# Capability → risk guidance (informational, not enforced)
CAPABILITY_RISK_GUIDANCE: dict[str, str] = {
    "broadcast":        "HIGH — sends to multiple recipients; hard to recall",
    "delete_message":   "HIGH — irreversible on most platforms",
    "trigger_workflow": "HIGH — may trigger irreversible CI/CD pipelines",
    "platform_shortcut": "HIGH — arbitrary automation on local machine",
    "send_email":       "HIGH — sends external communication",
    "read_vault_item":  "LOW — metadata only; never exposes secret values",
    "get_item_reference": "LOW — returns URI reference only",
}
