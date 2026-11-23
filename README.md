# MA Debrid

A custom [Music Assistant](https://music-assistant.io) `MusicProvider` that recreates the
on-demand workflow of the Eclipse Android app: Deezer provides accurate search metadata, Prowlarr
finds releases, TorBox confirms which releases are cached, and the matching audio file is streamed
straight from a TorBox direct link. Nothing is downloaded to disk first.

## How it works

1. **Search** - Music Assistant's search box queries the public Deezer API
   (`/search/track` and `/search/album`) and gets back real titles, artists, album art, durations
   and ISRCs, mapped to Music Assistant `Track` / `Album` models using Deezer ids.
2. **Track lookup** - Before playback Music Assistant resolves the full track via `/track/{id}`.
3. **Indexer search** - `get_stream_details` searches Prowlarr's `/api/v1/search` for
   `"{artist} {title}"`, then falls back to `"{artist} {album}"`, restricted to the Audio
   categories `3000`, `3010` and `3040`.
4. **Cache check** - All found infohashes are checked in one TorBox
   `torrents/checkcached` call with `format=list&list_files=true`.
5. **File matching** - Cached torrents are ranked by the `preferred_quality` setting. Inside an
   album pack the specific track is located by fuzzy title matching plus track number, so a
   single track resolves from a full-album torrent. `.cue`, `.log`, artwork and sample files are
   never selected.
6. **Direct link** - The chosen torrent is added with `add_only_if_cached=true`, its file id is
   read from the account listing, and `torrents/requestdl` returns the CDN URL that Music
   Assistant streams over HTTP.

## Requirements

- A TorBox account/plan that allows torrents and direct downloads.
- A reachable Prowlarr instance with audio indexers (Torznab/Newznab).
- No extra Python packages: the provider uses only Music Assistant's own HTTP session and
  models.

## Install into a Dockerized Music Assistant

### The container folder must be named `ma_debrid`

Music Assistant imports every provider by its manifest `domain`:

```python
import music_assistant.providers.ma_debrid
```

The folder inside the container therefore has to be `ma_debrid`. The folder on your host can
still be called `MA Debrid`; Docker maps host folder to container folder in the volume line:

```yaml
- "./MA Debrid:/app/venv/lib/python3.14/site-packages/music_assistant/providers/ma_debrid"
```

A container folder named `MA Debrid` will be ignored, because Python cannot import that name.

### 1. Find the providers folder in your container

The absolute path depends on the image and its Python version. Print it from the running
container:

```bash
docker exec -it music-assistant \
  /app/venv/bin/python -c "import music_assistant, pathlib; print(pathlib.Path(music_assistant.__file__).parent / 'providers')"
```

The command prints something like
`/app/venv/lib/python3.14/site-packages/music_assistant/providers`.

### 2. Add the bind mount

Replace `<providers-folder>` below with the path printed in step 1 and keep the trailing
`/ma_debrid`.

#### docker compose

```yaml
services:
  music-assistant:
    image: ghcr.io/music-assistant/server:latest
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./data:/data
      - "./MA Debrid:/app/venv/lib/python3.14/site-packages/music_assistant/providers/ma_debrid"
```

The right-hand path must be `<providers-folder>/ma_debrid`; the example shows the common
Python 3.14 layout.

#### docker run

```bash
docker run -d \
  --name music-assistant \
  --network host \
  --restart unless-stopped \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/MA Debrid:/app/venv/lib/python3.14/site-packages/music_assistant/providers/ma_debrid" \
  ghcr.io/music-assistant/server:latest
```

Recreate the container (`docker compose up -d` or the `docker run` above) so the mount is
applied.

### 3. Check that Music Assistant sees it

```bash
docker exec -it music-assistant \
  /app/venv/bin/python -c "import pathlib, music_assistant; p=pathlib.Path(music_assistant.__file__).parent/'providers'/'ma_debrid'; print(p, p.is_dir())"
```

Expect `True`. Then restart Music Assistant and add the provider in the UI:

**Settings → Providers → Add provider → MA Debrid**.

After a Music Assistant image upgrade the Python version in the path may change. Re-run step 1
and update the mount if needed.

## Configuration

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| TorBox API key | Secure string, required | - | Bearer token used for cache checks, torrent adds and download links. |
| Prowlarr URL | String, required | - | Base URL reachable **from the container**, e.g. `http://192.168.1.10:9696`. |
| Prowlarr API key | Secure string, required | - | Found in Prowlarr under Settings → General. |
| Preferred audio quality | String | `flac` | `flac`, `mp3` or `any`; used to rank cached formats. |

The Prowlarr URL must be reachable from inside the Music Assistant container. `localhost` there
points at the container itself, so use the host's LAN address instead.

## Test it

### Automated tests

The suite is standalone: it mocks all HTTP responses and needs no Music Assistant server or
credentials. From this folder:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install music-assistant-models aiohttp pytest pytest-asyncio
.venv/Scripts/python -m pytest -q
```

On Linux/macOS use `.venv/bin/python` instead of `.venv/Scripts/python`. Expected result:

```text
11 passed
```

What is covered:

- Deezer track/album parsing into Music Assistant `Track` and `Album` models (artists, artwork,
  provider mappings, ISRC).
- Selecting the exact track inside a multi-track album pack from a TorBox `checkcached`
  response, quality preference, and exclusion of `.cue`/`.log`/sample files.
- End-to-end `StreamResolver.resolve` returning `StreamDetails` with `StreamType.HTTP` and a
  TorBox CDN path, plus the `MediaNotFoundError` path for an empty cache.

### Live check through Music Assistant

1. Confirm the provider is listed under **Settings → Providers → Add provider**.
2. Add it, paste the TorBox and Prowlarr values, and save. A wrong key shows up as a provider
   error in the UI; check `docker logs music-assistant` for details.
3. Search for a track, open it, and press play. The provider only appears in search results
   once configured.
4. Watch the log while playing:

   ```bash
   docker logs -f --since 5m music-assistant 2>&1 | grep -iE "ma_debrid|MA Debrid|error"
   ```

### Check Prowlarr reachability from inside the container

```bash
docker exec -it music-assistant \
  /app/venv/bin/python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://192.168.1.10:9696/api/v1/system/status', headers={'X-Api-Key':'YOUR_KEY'})).status)"
```

A `200` means the URL and key work from the container. A connection error means the URL is not
reachable from inside Docker (common when `localhost` or `127.0.0.1` was used).

## Troubleshooting

- **MA Debrid is not in the Add provider list** - the mount path or container folder name is
  wrong. The container folder must be `<providers-folder>/ma_debrid`, the mount must exist when
  the container starts, and Music Assistant must have been restarted after mounting. Check the
  step 3 command and `docker logs music-assistant`.
- **Provider loads but saving the settings fails** - a required field is missing or the Prowlarr
  URL does not start with `http://` or `https://`.
- **401/403 from TorBox or Prowlarr** - check the API keys; Prowlarr keys are under
  Settings → General, TorBox keys under Account → API.
- **"No cached release contains a matching file"** - TorBox has no cached torrent with that exact
  track for this artist, or the indexers only returned uncached releases. Try a more popular
  release, check the audio categories in Prowlarr, or switch `preferred_quality` to `any`.
- **Playback starts and then fails** - TorBox direct links are short-lived; Music Assistant
  requests a new one for each playback. If it keeps failing, verify the torrent still exists in
  the TorBox account and that the plan allows downloading it.

## Limitations

- TorBox direct links are time-limited; Music Assistant requests a fresh link for every playback.
- Every played track is added to the TorBox account (instantly, because it is cached), so torrents
  accumulate there. Remove old ones in TorBox if the list grows.
- Album search returns metadata, but only tracks can be streamed. Playback of an album goes
  through its individual tracks.
- Deezer's public API is used for metadata only; audio always comes from TorBox.
- No library sync, playlists or radio: the provider declares `ProviderFeature.SEARCH` only.
