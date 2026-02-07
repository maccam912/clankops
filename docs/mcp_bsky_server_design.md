MCP Server for Blue Sky (AT Protocol) - Design Document

1. OVERVIEW
==========
Purpose: Build an MCP (Model Context Protocol) server that exposes Blue Sky social network functionality to AI assistants through standardized tools and resources. This allows assistants to interact with Blue Sky accounts, read timelines, post content, manage follows, etc., without needing direct API integration.

Target Users: AI assistants (like ClankOps) that need to publish to, monitor, or interact with Blue Sky as part of their workflows.

Key Design Principle: Keep authentication secure, handle rate limits gracefully, and provide clear, consistent tool interfaces.

2. ARCHITECTURE
==============
Components:
- Auth Manager: Handles OAuth with AT Protocol, token storage/refresh
- API Client: Wrapper around AT Protocol HTTP endpoints (using atproto library or direct HTTP)
- Tool Registry: MCP tools exposed (list, call, describe)
- Resource Registry: Readable resources (timeline, profile, etc.)
- Rate Limiter: Tracks and enforces AT Protocol rate limits per session
- Error Handler: Converts AT Protocol errors into MCP error responses
- Config Loader: Reads server configuration (credentials, scopes, etc.)

Flow:
1. Server starts, loads configuration
2. MCP connection established, lists available tools/resources
3. When a tool is called, server ensures auth (lazy login if needed)
4. Execute AT Protocol request via API client
5. Return structured result to MCP client

3. AUTHENTICATION & SECURITY
============================
OAuth Flow:
- Use AT Protocol's OAuth with PKCE
- Server acts as OAuth client; user authorizes via Blue Sky app
- Store refresh token securely (encrypted at rest if possible)
- Access tokens auto-refresh when expired
- Credentials stored per-server, not per-tool-call

Session Management:
- One Blue Sky identity per MCP server instance (multi-tenant not needed)
- Auth state persisted to a local file (e.g., ~/.mcp_bsky_credentials.json)
- Optional: allow switching identities via configuration

Security:
- Never log access tokens or PII from posts
- Use HTTPS only
- Validate redirect URIs
- Store credentials with restrictive permissions

4. MCP TOOLS TO EXPOSE
=======================
Authentication:
- bsky_login: Initiate OAuth flow, get auth URL
- bsky_logout: Clear stored credentials

Reading:
- bsky_get_timeline: Get home timeline (posts from follows)
- bsky_get_author_feed: Get posts from a specific author
- bsky_search_posts: Search posts by text
- bsky_get_profile: Get user profile (display name, bio, avatar)
- bsky_get_follows: List who a user follows
- bsky_get_followers: List followers

Writing:
- bsky_post: Create a new post (text, optional images via external upload)
- bsky_reply: Reply to a post
- bsky_like: Like a post
- bsky_repost: Repost
- bsky_delete: Delete own post
- bsky_follow: Follow a user
- bsky_unfollow: Unfollow a user

Management:
- bsky_get_notifications: Get recent notifications
- bsky_mute: Mute user/post
- bsky_block: Block user

Image Handling:
- For posts with images: upload to Blue Sky's blob storage first, then include blob ref in post. This may require an extra tool: bsky_upload_image.

5. RESOURCES TO EXPOSE
=====================
Resources provide read-only access to data without explicit tool calls:
- bsky://timeline/home: The user's home timeline
- bsky://profile/{handle}: User's profile
- bsky://post/{uri}: Single post by URI
- bsky://notifications: Recent notifications

These can be subscribed to by the MCP client for automatic updates (if supported).

6. DATA MODELS
=============
Standardized return formats for all tools:
- Success: { "success": true, "data": { ... } }
- Error: { "success": false, "error": "message", "code": "error_code" }

Post structure:
{
  "uri": "at://...",
  "cid": "...",
  "author": "did:plc:...",
  "text": "...",
  "created_at": "ISO8601",
  "embed": { ... } | null,
  "reply_count": int,
  "repost_count": int,
  "like_count": int,
  "liked_by_me": bool,
  "reposted_by_me": bool
}

Profile structure:
{
  "did": "...",
  "handle": "...",
  "display_name": "...",
  "description": "...",
  "avatar": "URL",
  "followers_count": int,
  "follows_count": int,
  "is_verified": bool
}

7. ERROR HANDLING
=================
Map AT Protocol errors to MCP error codes:
- 429 Rate Limit: return code "rate_limited", include retry_after seconds
- 401/403 Auth error: trigger re-auth flow or return "unauthorized"
- 404 Not Found: "not_found"
- 400 Bad Request: "invalid_input"
- Server errors (5xx): "upstream_error", possibly retryable

Tools should never crash; always return a structured error.

8. RATE LIMIT MANAGEMENT
=========================
AT Protocol enforces quotas (e.g., 5000 posts/day, 200 follows/day, etc.).
Server should:
- Track usage per limit (reset times vary)
- Before writing actions, check remaining quota
- If limit exceeded, return clear error with quota info
- For reads, respect rate limits but usually less strict

Optionally provide a tool: bsky_get_rate_limits to return current quota status.

9. CONFIGURATION
================
Configuration file (e.g., ~/.mcp_bsky_config.json):
{
  "service_url": "https://bsky.social",  // or custom PDS
  "client_id": "...",  // from Blue Sky app registration
  "client_secret": "...",
  "redirect_uri": "http://localhost:8080/callback",
  "scopes": ["com.atproto.social.write", "com.atproto.social.read"],
  "credentials_file": "~/.mcp_bsky_credentials.json"
}

Server could also read credentials from environment variables for Docker deployments.

10. IMPLEMENTATION CONSIDERATIONS
=================================
Language: Python (aligned with existing stack)
Dependencies:
- mcp (official Python SDK)
- atproto (official AT Protocol library) or httpx for direct HTTP
- cryptography (for encrypting stored credentials if needed)
- pydantic (for data validation)

Project structure:
mcp_bsky/
  __init__.py
  server.py      # MCP server entry, tool/resource registration
  auth.py        # OAuth flow, token storage/refresh
  client.py      # AT Protocol API wrapper
  config.py      # Configuration loading/validation
  models.py      # Pydantic models for posts, profiles, etc.
  limits.py      # Rate limit tracking
  cli.py         # Standalone server binary

Testing:
- Mock AT Protocol responses
- Test auth flow, rate limit handling, error cases
- Integration test against a test PDS if available

Deployment:
- Can run as standalone process
- Optionally embed into existing agent runtime

11. FUTURE EXTENSIONS
=====================
- Support multiple identities (config切换)
- Upload images/videos (requires blob upload)
- Rich text facets (AT Protocol has fancy text handling)
- Moderation tools (hide/delete reports)
- Feed customization (custom algorithm selection)
- Graph queries (search users, search by hashtag)
- Event stream: subscribe to live updates via WebSocket (if AT Protocol supports)

12. OPEN QUESTIONS
==================
- Should we support custom PDS URLs? Likely yes.
- How to handle image uploads in a tool? Perhaps separate tool for upload returning blob ref.
- Should resources be updated automatically? MCP clients may poll; we can provide a timestamp to check freshness.
- Error messages: be terse for Telegram, but detailed when requested? Could have a "verbose" option.

13. ASSUMPTIONS
===============
- One identity per server is sufficient for typical assistant use
- AT Protocol API is stable enough
- The MCP client can handle OAuth redirect flow (or we provide a manual copy-paste code method if no local server)
- Rate limits are high enough for assistant-driven usage (not a spam bot)

14. SUCCESS CRITERIA
===================
- Can authenticate and obtain tokens
- Can post a simple text post
- Can read home timeline
- Errors are handled gracefully with clear messages
- Rate limits are respected and reported
- Server is stateless except for credentials; can restart without losing auth
- Tools conform to MCP spec and are self-documenting

15. DEVELOPMENT PLAN
====================
Phase 1: Setup and Auth
- Register a Blue Sky app to get client_id/secret
- Implement OAuth PKCE flow with local server callback
- Store/refresh tokens

Phase 2: Basic Tools
- Implement client wrapper for core endpoints
- Add bsky_post, bsky_get_timeline, bsky_get_profile
- Test end-to-end

Phase 3: More Tools
- Add interactions (like, repost, follow)
- Add search
- Add error handling and rate limiting

Phase 4: Polish
- Add resource URIs
- Thorough testing
- Documentation and usage guide

Phase 5: Advanced
- Image uploads
- Multi-PDS support
- Configurable verbosity
