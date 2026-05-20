# byparr-proxy

A 70-line HTTP passthrough that gets **1337x and other Cloudflare-protected indexers working again with Prowlarr/Jackett in 2026**, when FlareSolverr and Byparr alone are no longer enough.

If you've been seeing this in Prowlarr's logs even with FlareSolverr or Byparr configured:

```
NzbDrone.Core.Http.CloudFlare.CloudFlareProtectionException:
  Unable to access 1337x.to, blocked by CloudFlare Protection.
```

...this is for you.

---

## The problem

By early 2026, Cloudflare's protection of sites like 1337x has tightened to the point where the entire **FlareSolverr/Byparr cookie-replay model is fundamentally broken**. The mechanism that breaks:

1. Prowlarr makes a direct HTTPS request to `https://1337x.to/cat/Movies/1/`.
2. Cloudflare returns a "Just a moment..." 403 challenge page.
3. Prowlarr detects the challenge and sends the URL to FlareSolverr/Byparr via its indexer-proxy mechanism.
4. Byparr fires up a real Firefox, solves the JavaScript challenge, and returns a `cf_clearance` cookie plus the actual page HTML.
5. **Prowlarr throws away the HTML** and replays the request to `https://1337x.to/cat/Movies/1/` with the cookie attached, using its own .NET `HttpClient`.
6. Cloudflare inspects the replay — different TLS/JA4 fingerprint than the one that solved the challenge, slightly different HTTP/2 settings, extra headers like `Accept-Encoding: gzip` — and reissues a fresh challenge.
7. Prowlarr sees the new challenge → throws `CloudFlareProtectionException`. Fail.

The cookie was valid. Byparr worked. The solver is not the problem. The problem is the **replay step (5)** — Prowlarr discarding the already-fetched body and re-fetching from a client Cloudflare can fingerprint.

Workarounds we tried that **did not** fix it:
- Updating Prowlarr to the latest version
- Updating Byparr to the latest version
- Disabling IPv6 on the host network adapter
- Patching the Cardigann YAML to add `Accept: */*` and matching `User-Agent` headers ([FlareSolverr issue #1672](https://github.com/FlareSolverr/FlareSolverr/issues/1672)'s suggested fix)
- Switching from FlareSolverr to Byparr
- Switching from Prowlarr to Jackett (Jackett does the same cookie replay and fails the same way)

The body returned by Byparr always contained the real torrent listings. Prowlarr just never used it.

## The fix

Stop the replay from ever happening. Put a tiny HTTP server between Prowlarr and the indexer that **uses Byparr's response body directly**:

```
┌──────────┐   plain HTTP    ┌──────────────┐   POST /v1   ┌────────┐   real browser   ┌─────────┐
│ Prowlarr │ ──────────────► │ byparr-proxy │ ───────────► │ Byparr │ ───────────────► │ 1337x   │
│          │ ◄────── 200 OK  │              │ ◄── 200 OK   │        │ ◄── solved HTML  │ (via CF)│
└──────────┘   real HTML     └──────────────┘   solution   └────────┘                  └─────────┘
```

Prowlarr's 1337x indexer is reconfigured to use `http://byparr-proxy:8888/` as its **Base URL**. Prowlarr never talks to Cloudflare directly. The Cloudflare detection in Prowlarr's `CloudFlareDetectionService` never fires, because Prowlarr only ever sees clean `200 OK` responses with real HTML in the body. There is no cookie replay because there is no second request to replay.

That's it. ~70 lines of Python, no extra dependencies, reuses the Byparr you already have running.

## Quick start

Prerequisites:
- Docker + Docker Compose
- Prowlarr (or Jackett) running in the same Docker network
- Byparr running (`ghcr.io/thephaseless/byparr:latest`)

### 1. Build and run

```bash
git clone this repo
cd byparr-proxy
docker compose up -d
```

This starts both `byparr` and `byparr-proxy`. If you already run Byparr in your existing arr stack, drop just the snippet below into your existing compose instead.

Smoke-test from your browser: visit `http://YOUR_HOST_IP:8888/cat/Movies/1/`. After 30s–2min (Byparr solving Cloudflare for the first time — can be slow under load), you should see the actual 1337x Movies page render, unstyled, because we 404 static assets to keep things fast.

### Drop-in snippet for an existing arr-stack compose

If you're already running Byparr in your compose, just append this one service. No other changes needed — it'll join your existing Docker network and reach Byparr by service name.

```yaml
  byparr-proxy:
    image: ghcr.io/YOUR_USER/byparr-proxy:latest  # or `build: ./byparr-proxy` if cloned locally
    container_name: byparr-proxy
    restart: unless-stopped
    ports:
      - "8888:8888"
    environment:
      UPSTREAM: https://1337x.to
      BYPARR: http://byparr:8191/v1
      TIMEOUT_MS: "120000"
      PORT: "8888"
    depends_on:
      - byparr
```

Then `docker compose up -d byparr-proxy` and continue with step 2.

### 2. Drop the Cardigann definition into Prowlarr

**a. Create the `Custom/` folder if it doesn't exist.** It often isn't there by default — Prowlarr only creates it once you've actually added a custom definition through some prior workflow. From your Docker host (Linux example shown; on Windows use `mkdir` instead):

```bash
mkdir -p <prowlarr config volume>/Definitions/Custom
```

**The `Custom/` subdirectory is required** — Prowlarr (recent versions) ignores YAML files placed directly in `Definitions/`. Those are its own internal mirror of built-in definitions; user customs only get loaded from `Custom/`. If you skip this step or drop the YAML one folder up, the indexer will silently not appear in the add list.

**b. Copy `definitions/1337x-byparr.yml` into that folder:**

```
<prowlarr config volume>/Definitions/Custom/1337x-byparr.yml
```

Verify from inside the container that Prowlarr can see the file:
```bash
docker exec prowlarr sh -c "ls /config/Definitions/Custom/ && head -5 /config/Definitions/Custom/1337x-byparr.yml"
```
You should see `1337x-byparr.yml` listed and its first 5 lines (with `id: 1337x-byparr`).

**c. Restart Prowlarr:**

```bash
docker restart prowlarr
```

### 3. Add the indexer in Prowlarr

1. Open Prowlarr UI.
2. **Indexers → + Add Indexer**.
3. Search for **"1337x (via Byparr)"** in the list and click it.
4. Base URL is automatically `http://byparr-proxy:8888/` (the only entry in the YAML).
5. **Do not add a FlareSolverr tag.** That's the whole point — we don't want Prowlarr's indexer-proxy mechanism kicking in.
6. Click **Test**. First test typically takes 30s–2min while Byparr solves (sometimes longer if the server is under load). Should green-check.
7. **Save**.

If the indexer doesn't appear in the add list, see [Troubleshooting](#troubleshooting).

### ⚠️ A note on search latency

Each uncached search through this proxy takes 30 seconds to 2 minutes (Byparr is solving Cloudflare freshly). The timeouts that matter are all internal and not user-configurable in modern UIs:

- **`byparr-proxy` itself** — we set `TIMEOUT_MS=120000` (2 min) above. This is the only knob you control directly. Bump it higher if you regularly see `byparr request failed: timed out` in `docker logs byparr-proxy`.
- **Prowlarr's indexer HTTP timeout** — internal (~100s+), not exposed in the UI.
- **Radarr / Sonarr → Prowlarr** — internal, generous by default.

In practice you do **not** need to change anything in Radarr or Sonarr — they just wait. The user-facing symptom of a too-slow solve is a single failed RSS sync or manual search that retries on its own next cycle. If you see *consistent* timeouts (multiple failures in a row, indexer marked unhealthy), the real fix is reducing concurrent load on Byparr — not raising timeouts further. Lower Radarr/Sonarr's interactive search concurrency, or stagger RSS sync intervals.

## Adding more indexers

The proxy is single-upstream per instance — one container handles one site. To add a second indexer (e.g. YTS):

**1. Add another `byparr-proxy-yts` service to your compose:**

```yaml
  byparr-proxy-yts:
    build: .
    container_name: byparr-proxy-yts
    restart: unless-stopped
    ports:
      - "8889:8889"
    environment:
      UPSTREAM: https://yts.mx
      BYPARR: http://byparr:8191/v1
      TIMEOUT_MS: "60000"
      PORT: "8889"
    depends_on:
      - byparr
```

**2. Create a Cardigann YAML at `<prowlarr config>/Definitions/Custom/yts-byparr.yml`** based on the existing `yts.yml` from the [Prowlarr indexers repo](https://github.com/Prowlarr/Indexers), with two changes:
   - Change `id:` to something unique (`yts-byparr`)
   - Change `name:` to something distinguishable (`YTS (via Byparr)`)
   - Replace the `links:` block with a single entry: `- http://byparr-proxy-yts:8889/`

**3. Restart Prowlarr, add the new indexer through the UI.**

## Configuration reference

The proxy reads four environment variables:

| Variable     | Default                      | Description                                              |
| ------------ | ---------------------------- | -------------------------------------------------------- |
| `UPSTREAM`   | `https://1337x.to`           | Base URL of the indexer the proxy should fetch from.     |
| `BYPARR`     | `http://byparr:8191/v1`      | Byparr's `/v1` endpoint, reachable from the container.   |
| `TIMEOUT_MS` | `120000`                     | Per-request timeout passed to Byparr (milliseconds). Bump higher if solves regularly exceed 2 minutes. |
| `PORT`       | `8888`                       | Local port the proxy listens on.                         |
| `LOG_LEVEL`  | `INFO`                       | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Set to `DEBUG` to see Byparr's solved URL and byte counts per request. |

### Reading the logs

Every request gets a 6-character request ID so concurrent requests stay legible:

```
2026-05-20 21:02:15.430 INFO  [a1b2c3] GET /cat/Movies/1/ from 172.18.0.7 -> forwarding to byparr (https://1337x.to/cat/Movies/1/)
2026-05-20 21:02:38.541 INFO  [a1b2c3] GET /cat/Movies/1/ <- 200 in 23.11s (byparr 23.09s, 31504 bytes)
2026-05-20 21:02:39.100 INFO  [d4e5f6] GET /css/style.css from 172.18.0.7 -> 404 skipped (static asset)
2026-05-20 21:03:00.000 ERROR [g7h8i9] GET /cat/Movies/2/ <- byparr request failed after 60.52s: timed out
```

What to look for when something's wrong:
- Long `byparr` times (>60s) on every request: Byparr is overloaded or being challenged hard. Reduce concurrent searches.
- `byparr returned non-ok`: Byparr itself failed to solve. Check `docker logs byparr`.
- `byparr request failed: timed out`: the proxy is waiting longer than `TIMEOUT_MS`. Bump it if real solves consistently exceed it.
- Bytes < 5000 on a 200 response: Byparr may be returning a small error/challenge page instead of real content. Spot-check the body manually.

## Limitations

- **GET and HEAD only.** Indexer search/listing is GET; the proxy does not currently forward POST bodies. Most Cardigann definitions use GET, so this is rarely an issue.
- **The actual `.torrent` file or magnet link** is not fetched via the proxy. The Cardigann YAML's `download:` section gets a magnet URI or an `itorrents.org` URL, both of which Prowlarr fetches directly and which are not Cloudflare-protected.
- **The proxy returns Byparr's HTML body verbatim.** It does not rewrite internal links. The Cardigann definition only cares about path-based selectors, so this works fine — but don't try to use the proxy as a general-purpose browser front-end.
- **First request to a domain is slow** — typically 30s to 2 minutes while Byparr solves, occasionally longer. Subsequent requests within the cf_clearance lifetime are usually faster, but Byparr's session reuse is best-effort.
- **Static assets (.css/.js/.png/.svg/etc.) get an instant 404** from the proxy by design. Prowlarr never requests them; this just keeps browser smoke-tests from triggering pointless Cloudflare solves.

## Troubleshooting

**"The new indexer doesn't appear in Prowlarr's add list after restart"**

Check that the file is actually in the `Custom/` subdirectory and readable inside the container:

```bash
docker exec prowlarr sh -c "ls /config/Definitions/Custom/ && head -5 /config/Definitions/Custom/1337x-byparr.yml"
```

You should see `1337x-byparr.yml` listed, with the top of the file showing `id: 1337x-byparr`. If it's not in `Custom/`, move it there — Prowlarr ignores YAMLs placed directly in `Definitions/`.

**"Test fails with `byparr request failed: Request Timeout`"**

The container can't reach Byparr. Confirm:
1. Both containers are on the same Docker network.
2. Byparr is running: `docker ps | grep byparr`.
3. From inside the proxy container, Byparr is reachable: `docker exec byparr-proxy python -c "import urllib.request; print(urllib.request.urlopen('http://byparr:8191/health').read())"`.

**"Test fails with `byparr returned: Cloudflare challenge not detected`" or similar**

Byparr itself is failing. This means the indexer's Cloudflare config is currently beating Byparr too — not something this proxy can fix. Check the Byparr issue tracker and consider whether the indexer has an `.onion` mirror you can use via a Tor proxy instead.

**"Search works but Prowlarr says 0 results"**

The Cardigann YAML's selectors didn't match anything. The site may have changed its HTML. Compare the current site structure against the `fields:` block in the YAML. Update the selectors, or pull the latest definition from the [Prowlarr indexers repo](https://github.com/Prowlarr/Indexers) as a starting point.

## Why this works when other workarounds don't

| Approach                                   | Outcome                                                            |
| ------------------------------------------ | ------------------------------------------------------------------ |
| FlareSolverr alone                         | Cookie replay caught by Cloudflare → 403                           |
| Byparr alone (drop-in for FlareSolverr)    | Same cookie replay path → 403                                       |
| Add `Accept: */*` headers to Cardigann YAML | Some sites yes, 1337x no — Cloudflare uses more than just headers   |
| Switch to Jackett                          | Same cookie replay architecture → same failure                      |
| Disable IPv6                               | Only helps when CF binds cookies to v6 but client replays over v4   |
| **byparr-proxy (this project)**            | **No replay path exists. Cloudflare detection in Prowlarr never fires.** |

## Acknowledgements

- [Byparr](https://github.com/ThePhaseless/Byparr) — does the actual Cloudflare-solving with a real browser. This project is just a wrapper.
- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — the project Byparr forked from. The [issue #1672 discussion](https://github.com/FlareSolverr/FlareSolverr/issues/1672) was where the root-cause analysis started.
- [Prowlarr](https://github.com/Prowlarr/Prowlarr) and the [Prowlarr Indexers repo](https://github.com/Prowlarr/Indexers) — the Cardigann definition for 1337x is adapted from there.
