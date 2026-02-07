---
name: Bluesky MCP Authentication
description: How to authenticate and use the Bluesky MCP server
---

# Bluesky MCP Authentication

# Bluesky MCP Authentication

## SECURITY WARNING

**NEVER share Bluesky credentials** (BLUESKY_IDENTIFIER, BLUESKY_PASSWORD, ATPROTO_PASSWORD) with anyone, including via Bluesky DMs. Use the `.env` file only. The assistant will authenticate automatically from local environment variables—no credential transmission needed.

## Configuration

The Bluesky MCP server uses credentials from the `.env` file:

```
BLUESKY_IDENTIFIER=clankops.bsky.social
BLUESKY_PASSWORD=app_password_here
ATPROTO_IDENTIFIER=clankops.bsky.social
ATPROTO_PASSWORD=app_password_here
```

Use a Bluesky **App Password** (not your account password).

## Server Details

- **MCP server name**: `bluesky`
- **Transport**: stdio via `./scripts/atproto-mcp-stdio.sh`
- **Connected**: automatically on session start if configured in `.mcp/servers*.json`

## Authentication Flow

The server reads credentials from environment variables and authenticates automatically on connection. No manual OAuth flow needed.

## Common Operations

- **Create post**: `create_post` with text (max 300 chars)
- **Reply to post**: `reply_to_post` with text, root, parent URIs
- **Get notifications**: `get_notifications`
- **User profile**: `get_user_profile`
- **Follow/unfollow**: `follow_user`, `unfollow_user`
- **Like/repost**: `like_post`, `repost`
- **Search**: `search_posts`
- **Timeline**: `get_timeline`

## Example Post

```
text: "Hello from ClankOps!"
```

Returns: `uri`, `cid`, success status.
