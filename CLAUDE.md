# signal-gateway — notes for Claude

Docker-compose project that bridges other local containers to the operator's Signal account. Public docs in `README.md`; this file is for future Claude instances working on the code.

This project is published publicly on GitHub at `github.com/freducom/signal-gateway`. Keep all docs, code, and examples generic — no absolute host paths, personal phone numbers, country-specific defaults, real names, emails, or other PII. Use clearly fictional placeholders (`+15551234567`, `myapp`, etc.).

The GitHub owner `freducom` is baked into the published image refs (`ghcr.io/freducom/signal-gateway-*`) and the LICENSE copyright line. That's the project's distribution identity, not personal info — leaving it concrete is required for the one-step copy-and-go install.

## What it is

Three services on a single internal compose network, all bound only to `127.0.0.1`:

| Service      | Port | Role                                                              |
|--------------|------|-------------------------------------------------------------------|
| `signal-api` | 8080 | `bbernhard/signal-cli-rest-api` in `MODE=json-rpc`. Owns the Signal session. State in `data/signal-cli/`. |
| `notify`     | 8090 | Tiny Flask app. `POST /notify` (text body + `X-Token`) → sends to Note-to-Self via signal-api. |
| `router`     | 8091 | Flask app + background websocket loop. `POST /register {prefix, webhook}` self-registration API. Reads incoming Note-to-Self messages from signal-api over websocket, dispatches by first word, replies to the user with the webhook's response. Routes persisted to `data/router/routes.json`. |

The gateway is a **linked secondary device** on the user's existing Signal account. There is no separate phone number. All traffic flows through "Note to Self".

## Distribution model

End users do **not** clone this repo. They copy `docker-compose.yml` and a `.env` file into a fresh directory and `docker compose up`. The `notify` and `router` images are pre-built and published to `ghcr.io/freducom/signal-gateway-{notify,router}` by `.github/workflows/publish.yml` on every push to `main` (as `:latest`) and on `v*` tags (as `:vX.Y.Z`). The workflow resolves the owner from `${{ github.repository_owner }}`, so forks publish under their own namespace automatically; only the image refs in `docker-compose.yml` and the inline snippet in `README.md` need updating if a fork wants its own copy-and-go path.

For maintainer development, `docker-compose.dev.yml` overrides those images with `build:` directives so you can iterate on code without round-tripping through CI:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Design decisions that must not be unwound silently

- **`MODE=json-rpc` on signal-api.** `normal` mode forks a fresh JVM per call (10–20 s cold start) and does not support real-time receive. Don't switch back to save a dependency.
- **Echo-loop filter in router.** Every outbound message comes back to the gateway as a `syncMessage.sentMessage` because that's how linked devices stay in sync. Router drops envelopes where `sourceDevice == OUR_DEVICE_ID`. Our device id is queried from signal-api's `/v1/devices/{number}` at startup, matched by the device name `signal-gateway` set during linking. Without this filter every notification triggers a phantom command. If you change the link device name, change the lookup too.
- **Self-registration over a static routes file.** Routes are owned by the apps, not by signal-gateway. `data/router/routes.json` is a cache for restart resilience, not the source of truth.
- **Per-route `auth_header` is optional but encouraged.** When the registered webhook URL points at `host.docker.internal:<port>` (cross-network), any other container with that host alias can hit the same URL and spoof messages. The `auth_header` field on `/register` lets the registering app pin a shared secret that the router forwards on every dispatch. Stored alongside the URL in `routes.json`; `GET /routes` returns `has_auth_header` only, never the value.
- **All ports `127.0.0.1` only.** Never bind to `0.0.0.0`. Never add Traefik labels. `signal-api`'s REST API has no auth and would let any reachable client send/receive Signal as the user.
- **Two separate tokens.** `NOTIFY_TOKEN` gates `/notify`, `ROUTER_TOKEN` gates `/register`. Different blast radii — one lets a process spam the user; the other lets a process hijack an app's incoming-command channel. Don't merge.
- **Note-to-Self only.** Send target and receive filter are both the user's own number. Multi-recipient support is explicitly out of scope.
- **End-user simplicity.** "Copy one compose file + one .env, done" is a hard constraint. Don't add steps to install. If a change makes install harder, push back or hide the complexity behind CI / a default.

## Out of scope (don't add unless asked)

- Multi-recipient / contact-based sending.
- Stateful / multi-turn conversation handling in the router.
- Retry queues or persistent outbound buffers.
- A web UI for managing routes.
- LAN or public exposure of any port. Anyone asking for this on principle is asking for the wrong thing — push back.

## Keep these docs in sync

When changes touch architecture, ports, env vars, endpoints, setup steps, image names, or any of the design constraints above, update **`README.md` and `CLAUDE.md` in the same change**. The `docker-compose.yml` snippet inlined in `README.md` must also stay in sync with the canonical `docker-compose.yml` file in the repo root. Stale setup docs are worse than missing ones — users follow them blindly during the one-time link flow and waste time debugging the doc, not the system.
