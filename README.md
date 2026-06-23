# signal-gateway

A small, self-hostable Docker stack that lets other `docker compose` projects on the same host send Signal messages to you and (optionally) receive commands back.

- **Send:** any container `POST`s a text body → it arrives in your Signal **Note to Self**.
- **Receive:** you type a prefixed command in Note to Self from your phone → the router dispatches it to the matching registered container and replies to you with the result.

The gateway uses your existing Signal account as a **linked secondary device** — like Signal Desktop. There is no separate phone number, no SMS-verification dance, no third-party service.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Host (127.0.0.1 only — never LAN-reachable)                │
│  :8080 → signal-api    bbernhard/signal-cli-rest-api        │
│  :8090 → notify        outbound helper (POST /notify)       │
│  :8091 → router        inbound dispatcher + registration    │
└─────────────────────────────────────────────────────────────┘
```

Three services on one internal Docker network. Only the three host port binds escape, all on `127.0.0.1`.

## Install (≈ 5 minutes)

You don't need to clone this repo. Two files in a fresh directory are enough.

### 1. Create a directory and drop in `docker-compose.yml`

```yaml
services:
  signal-api:
    image: bbernhard/signal-cli-rest-api:latest
    container_name: signal-gateway-api
    restart: unless-stopped
    environment:
      - MODE=json-rpc
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - ./data/signal-cli:/home/.local/share/signal-cli
    networks: [ internal ]

  notify:
    image: ghcr.io/freducom/signal-gateway-notify:latest
    container_name: signal-gateway-notify
    restart: unless-stopped
    depends_on: [ signal-api ]
    environment:
      - SIGNAL_API_URL=http://signal-api:8080
      - SIGNAL_NUMBER=${SIGNAL_NUMBER}
      - NOTIFY_TOKEN=${NOTIFY_TOKEN}
    ports:
      - "127.0.0.1:8090:8090"
    networks: [ internal, signal ]

  router:
    image: ghcr.io/freducom/signal-gateway-router:latest
    container_name: signal-gateway-router
    restart: unless-stopped
    depends_on: [ signal-api ]
    environment:
      - SIGNAL_API_URL=http://signal-api:8080
      - SIGNAL_API_WS_URL=ws://signal-api:8080
      - SIGNAL_NUMBER=${SIGNAL_NUMBER}
      - ROUTER_TOKEN=${ROUTER_TOKEN}
      - DEVICE_NAME=signal-gateway
    ports:
      - "127.0.0.1:8091:8091"
    volumes:
      - ./data/router:/data
    networks: [ internal, signal ]

networks:
  internal:
    driver: bridge
  # Shared bridge other compose projects join as external. They reach the
  # gateway by service hostname (signal-gateway-notify:8090 etc.) — no host
  # port hopping, no host.docker.internal needed.
  signal:
    name: signal
    driver: bridge
```

### 2. Drop in `.env` next to it

```dotenv
# Your Signal phone number in E.164 form (the account you'll link to).
SIGNAL_NUMBER=+15551234567

# Shared secrets. Generate two different values:
#   openssl rand -hex 16
NOTIFY_TOKEN=replace-me
ROUTER_TOKEN=replace-me-too
```

### 3. Start signal-api and link your Signal account

```bash
docker compose up -d signal-api
```

Open <http://127.0.0.1:8080/v1/qrcodelink?device_name=signal-gateway> in a browser. A QR code appears.

On your phone: **Signal → Settings → Linked devices → Link new device → scan the QR.**

### 4. Restart signal-api so it picks up the new account, then start the helpers

```bash
docker compose restart signal-api
docker compose up -d notify router
```

(`signal-cli`'s json-rpc daemon only spins up properly once an account exists, so the restart is required exactly once after linking.)

### 5. Verify

```bash
# Load the tokens from .env for easier copy-paste:
set -a && . ./.env && set +a

# Outbound:
curl -H "X-Token: $NOTIFY_TOKEN" \
     --data 'hello from gateway' \
     http://127.0.0.1:8090/notify
# → "hello from gateway" appears in Note to Self.

# Inbound: from your phone, type "test ping" in Note to Self.
# → you receive a reply: "no handler for prefix 'test'"
```

Done. Wire up other compose projects below.

## Using from another compose project

Other projects join the shared `signal` bridge network (created by signal-gateway) as external. Inside that network, the gateway is reachable at the hostnames `signal-gateway-notify` (port 8090) and `signal-gateway-router` (port 8091).

> **Linux note.** The `signal` network is the canonical way to integrate. Reaching the gateway via the host's `127.0.0.1` binds from another container will silently fail on Linux because signal-gateway only listens on the host loopback — the host port binds are for browser-based QR linking and host-side `curl`, not for cross-container traffic.

### Just sending notifications

```yaml
services:
  myapp:
    # ...your existing config...
    networks: [ default, signal ]
    environment:
      - NOTIFY_TOKEN=<same value as signal-gateway/.env>

networks:
  default:
  signal:
    external: true
```

Inside `myapp`:

```bash
curl -H "X-Token: $NOTIFY_TOKEN" \
     --data "build done on $(hostname)" \
     http://signal-gateway-notify:8090/notify
```

### Send + receive (register a command prefix)

Add a one-shot sidecar that registers your command prefix on startup. Replace `myapp` and `1234` with your service name and port.

```yaml
services:
  myapp:
    # ...your existing config...
    networks: [ default, signal ]

  myapp-signal-register:
    image: curlimages/curl
    restart: on-failure
    depends_on: [ myapp ]
    networks: [ signal ]
    environment:
      - ROUTER_TOKEN=<same value as signal-gateway/.env>
    command: >
      sh -c 'until curl -sf
              -H "X-Token: $$ROUTER_TOKEN"
              -H "Content-Type: application/json"
              -d "{\"prefix\":\"myapp\",\"webhook\":\"http://myapp:1234/cmd\"}"
              http://signal-gateway-router:8091/register;
            do sleep 5; done'

networks:
  default:
  signal:
    external: true
```

`myapp`'s `/cmd` endpoint receives requests shaped like `POST {"message": "<text after the prefix>"}` and may return any text body — whatever it returns is sent back to you over Signal.

Anything else attached to the `signal` network can hit `myapp:1234/cmd` directly and spoof commands. To pin the call to signal-gateway, register with an `auth_header` and have `myapp` reject any `/cmd` request whose `X-Webhook-Token` header doesn't match:

```yaml
    command: >
      sh -c 'until curl -sf
              -H "X-Token: $$ROUTER_TOKEN"
              -H "Content-Type: application/json"
              -d "{
                \"prefix\":\"myapp\",
                \"webhook\":\"http://myapp:1234/cmd\",
                \"auth_header\":{\"name\":\"X-Webhook-Token\",\"value\":\"$$WEBHOOK_TOKEN\"}
              }"
              http://signal-gateway-router:8091/register;
            do sleep 5; done'
```

## API reference

### notify — `127.0.0.1:8090`

| Method | Path     | Auth                     | Body     |
|--------|----------|--------------------------|----------|
| POST   | /notify  | `X-Token: $NOTIFY_TOKEN` | raw text |
| GET    | /health  | —                        | —        |

### router — `127.0.0.1:8091`

| Method | Path                | Auth                     | Body                                                                                              |
|--------|---------------------|--------------------------|---------------------------------------------------------------------------------------------------|
| POST   | /register           | `X-Token: $ROUTER_TOKEN` | `{"prefix": "...", "webhook": "...", "auth_header": {...}, "timeout_seconds": 30}` (auth + timeout optional) |
| DELETE | /register/{prefix}  | `X-Token: $ROUTER_TOKEN` | —                                                                                                 |
| GET    | /routes             | `X-Token: $ROUTER_TOKEN` | — (returns `webhook`, `has_auth_header`, `timeout_seconds`)                                       |
| GET    | /health             | —                        | —                                                                                                 |

If `auth_header` is supplied in `/register`, the router sends that header on every webhook POST. Use it when other containers share the `signal` network and could otherwise reach the webhook directly — the receiving app verifies the header to confirm the call came through signal-gateway.

`timeout_seconds` is how long the router waits for the webhook to reply before giving up (default 30, max 600). LLM-backed handlers should register with a generous value — 300 s is a reasonable starting point. Webhook dispatch runs in a thread pool, so a slow handler does not block other prefixes.

**Convention: handlers self-document.** Every webhook should respond to an empty message body (the user typed just the prefix, e.g. `bv` alone) with a plain-text listing of its commands / subcommands / features. Don't return 200 empty in that case — the router will substitute `ok` and the user is left guessing. The listing is the handler's contract with the user; only the handler knows what it supports.

### signal-api — `127.0.0.1:8080`

Raw [bbernhard/signal-cli-rest-api](https://bbernhard.github.io/signal-cli-rest-api/). Used for the QR link flow and any advanced operations. **No authentication** — see Security below.

## Security model

- All ports bind to `127.0.0.1` only. The gateway is **not** reachable from the LAN.
- `NOTIFY_TOKEN` and `ROUTER_TOKEN` are separate secrets that gate the helper endpoints from any other process that can reach them — anything on the shared `signal` network, or anything on the host that can dial `127.0.0.1:8090` / `127.0.0.1:8091`.
- `signal-api` on `:8080` is **unauthenticated**. Anything that can reach `127.0.0.1:8080` can send Signal messages as you and read your Note-to-Self traffic. Treat it as a privileged host-local interface.
- The blast radius of a leaked token is "spam yourself on Signal" — not external exposure.

## Troubleshooting

- **Gateway stopped working after a while.** Open Signal on your phone → Settings → Linked devices. If the `signal-gateway` device is gone (unlinked, expired), repeat step 3 of install.
- **Outbound messages never arrive.** Check `docker compose logs signal-api`. First-time send after a restart can take a few seconds while the json-rpc loop reconnects.
- **Router doesn't reply to messages I type on my phone.** Check `docker compose logs router`. Common causes: webhook target container is down, route was registered with a stale URL, signal-api websocket dropped.
- **Router replied to a message the gateway itself sent.** Should not happen — the router filters `sourceDevice == own device`. If you see it, restart `router` so it re-queries its own device id from signal-api.
- **My handler just returned 200 with no body — what does the user see?** The router replies `ok`. Non-2xx with no body surfaces as `({status} no body)`. Return a non-empty body if you want a richer reply.

## Development

If you want to modify the code instead of using the published images, clone the repo and use the dev override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Pushes to `main` and `v*` tags trigger `.github/workflows/publish.yml`, which builds both images for `linux/amd64` and `linux/arm64` and pushes them to `ghcr.io/<owner>/signal-gateway-{notify,router}`. The CI workflow resolves `<owner>` automatically from `${{ github.repository_owner }}`; if you fork and republish, change the image namespace in `docker-compose.yml` (and the inline snippet above) to match your fork.

## License

MIT — see [LICENSE](LICENSE).

## Out of scope (by design)

- Multi-recipient or contact-based sending — Note-to-Self only.
- Stateful / multi-turn conversations.
- Retry queues or persistent outbound buffers.
- A web UI for managing routes.
- Any LAN or public exposure.
