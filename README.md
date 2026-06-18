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
    networks: [ internal ]

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
    networks: [ internal ]

networks:
  internal:
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

### Just sending notifications

```yaml
services:
  myapp:
    # ...your existing config...
    extra_hosts:
      - host.docker.internal:host-gateway
    environment:
      - NOTIFY_TOKEN=<same value as signal-gateway/.env>
```

Inside `myapp`:

```bash
curl -H "X-Token: $NOTIFY_TOKEN" \
     --data "build done on $(hostname)" \
     http://host.docker.internal:8090/notify
```

### Send + receive (register a command prefix)

Add a one-shot sidecar that registers your command prefix on startup. Replace `myapp` and `1234` with your service name and port.

```yaml
services:
  myapp:
    # ...your existing config...
    extra_hosts:
      - host.docker.internal:host-gateway

  myapp-signal-register:
    image: curlimages/curl
    restart: on-failure
    depends_on: [ myapp ]
    extra_hosts:
      - host.docker.internal:host-gateway
    environment:
      - ROUTER_TOKEN=<same value as signal-gateway/.env>
    command: >
      sh -c 'until curl -sf
              -H "X-Token: $$ROUTER_TOKEN"
              -H "Content-Type: application/json"
              -d "{\"prefix\":\"myapp\",\"webhook\":\"http://myapp:1234/cmd\"}"
              http://host.docker.internal:8091/register;
            do sleep 5; done'
```

`myapp`'s `/cmd` endpoint receives requests shaped like `POST {"message": "<text after the prefix>"}` and may return any text body — whatever it returns is sent back to you over Signal.

## API reference

### notify — `127.0.0.1:8090`

| Method | Path     | Auth                     | Body     |
|--------|----------|--------------------------|----------|
| POST   | /notify  | `X-Token: $NOTIFY_TOKEN` | raw text |
| GET    | /health  | —                        | —        |

### router — `127.0.0.1:8091`

| Method | Path                | Auth                     | Body                                  |
|--------|---------------------|--------------------------|---------------------------------------|
| POST   | /register           | `X-Token: $ROUTER_TOKEN` | `{"prefix": "...", "webhook": "..."}` |
| DELETE | /register/{prefix}  | `X-Token: $ROUTER_TOKEN` | —                                     |
| GET    | /routes             | `X-Token: $ROUTER_TOKEN` | — (returns current registrations)     |
| GET    | /health             | —                        | —                                     |

### signal-api — `127.0.0.1:8080`

Raw [bbernhard/signal-cli-rest-api](https://bbernhard.github.io/signal-cli-rest-api/). Used for the QR link flow and any advanced operations. **No authentication** — see Security below.

## Security model

- All ports bind to `127.0.0.1` only. The gateway is **not** reachable from the LAN.
- `NOTIFY_TOKEN` and `ROUTER_TOKEN` are separate secrets that gate the helper endpoints from any other process on the host (including containers that have `host.docker.internal` access — which is most of them).
- `signal-api` on `:8080` is **unauthenticated**. Anything that can reach `127.0.0.1:8080` can send Signal messages as you and read your Note-to-Self traffic. Treat it as a privileged host-local interface.
- The blast radius of a leaked token is "spam yourself on Signal" — not external exposure.

## Troubleshooting

- **Gateway stopped working after a while.** Open Signal on your phone → Settings → Linked devices. If the `signal-gateway` device is gone (unlinked, expired), repeat step 3 of install.
- **Outbound messages never arrive.** Check `docker compose logs signal-api`. First-time send after a restart can take a few seconds while the json-rpc loop reconnects.
- **Router doesn't reply to messages I type on my phone.** Check `docker compose logs router`. Common causes: webhook target container is down, route was registered with a stale URL, signal-api websocket dropped.
- **Router replied to a message the gateway itself sent.** Should not happen — the router filters `sourceDevice == own device`. If you see it, restart `router` so it re-queries its own device id from signal-api.

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
