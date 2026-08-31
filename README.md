# Valheim Docker Mod Updater

A small updater for Docker-hosted Valheim servers that checks Thunderstore, downloads newer mod versions, backs up `BepInEx/config`, applies updates, and restarts the Docker container or Compose service only when updates are needed.

Running Valheim under CubeCoders AMP instead? Use the AMP-specific sibling project: [valheim-amp-mod-updater](https://github.com/tomtomdk/valheim-amp-mod-updater).

## What it does

- Reads Docker/server paths from `updater.settings.json`.
- Reads Thunderstore packages from `mods.json`.
- Checks installed versions using `state.json`.
- Downloads and extracts Thunderstore packages into the Valheim server folder.
- Supports package payloads under `BepInEx/`, `config/`, and top-level `patchers/`.
- Posts optional Discord webhook notifications if `discord_webhook` is set.
- Backs up `BepInEx/config` before applying updates.
- Leaves the container/service stopped if it was stopped before the updater ran.

## Files

- `thunderstore_sync.py` - Thunderstore resolver/downloader/deployer.
- `update_valheim_docker_mods.sh` - Docker/Compose-aware safe update wrapper for scheduled runs.
- `instant_update_valheim_docker_mods.sh` - Runs the same updater with no restart delay.
- `mods.example.json` - Example Thunderstore mod list.
- `updater.settings.example.json` - Example Docker/server path settings.

## Install

Copy the project to your Docker host, for example:

```bash
sudo mkdir -p /opt/valheim-modupdater
sudo cp thunderstore_sync.py update_valheim_docker_mods.sh instant_update_valheim_docker_mods.sh /opt/valheim-modupdater/
sudo cp mods.example.json /opt/valheim-modupdater/mods.json
sudo cp updater.settings.example.json /opt/valheim-modupdater/updater.settings.json
sudo chmod +x /opt/valheim-modupdater/*.sh /opt/valheim-modupdater/thunderstore_sync.py
```

Edit the config files:

```bash
sudo nano /opt/valheim-modupdater/updater.settings.json
sudo nano /opt/valheim-modupdater/mods.json
```

## Configure `updater.settings.json`

For a plain Docker container:

```json
{
  "docker": {
    "mode": "container",
    "container_name": "valheim",
    "docker_bin": "/usr/bin/docker"
  },
  "valheim": {
    "target_root": "/opt/valheim/server"
  }
}
```

For Docker Compose:

```json
{
  "docker": {
    "mode": "compose",
    "compose_file": "/opt/valheim/docker-compose.yml",
    "compose_project_dir": "/opt/valheim",
    "compose_service": "valheim",
    "docker_bin": "/usr/bin/docker"
  },
  "valheim": {
    "target_root": "/opt/valheim/server"
  }
}
```

The target root is the host path that contains `BepInEx/`. It should be the Valheim server files volume mounted into the container.

You can also point at another settings file for one run:

```bash
sudo SETTINGS_FILE=/path/to/updater.settings.json /opt/valheim-modupdater/update_valheim_docker_mods.sh
```

## Configure `mods.json`

Use Thunderstore package keys in `Owner-PackageName` format:

```json
{
  "discord_webhook": "",
  "mods": [
    "denikson-BepInExPack_Valheim",
    "ValheimModding-Jotunn"
  ]
}
```

Leave `discord_webhook` empty to disable Discord messages.

## Check Without Changing Anything

```bash
cd /opt/valheim-modupdater
./thunderstore_sync.py --config mods.json --target /path/to/valheim/root --check
```

Exit codes:

- `0` - no updates available
- `10` - updates available
- `2` - configuration or runtime error

## Run Updates

Scheduled/safe run with the configured delay:

```bash
sudo /opt/valheim-modupdater/update_valheim_docker_mods.sh
```

Immediate run with no warning delay:

```bash
sudo /opt/valheim-modupdater/instant_update_valheim_docker_mods.sh
```

Or:

```bash
sudo WAIT_SECONDS_OVERRIDE=0 /opt/valheim-modupdater/update_valheim_docker_mods.sh
```

## Optional systemd Timer

Create `/etc/systemd/system/valheim-docker-modupdate.service`:

```ini
[Unit]
Description=Update Docker Valheim Thunderstore mods

[Service]
Type=oneshot
ExecStart=/opt/valheim-modupdater/update_valheim_docker_mods.sh
```

Create `/etc/systemd/system/valheim-docker-modupdate.timer`:

```ini
[Unit]
Description=Run Docker Valheim mod updater daily

[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now valheim-docker-modupdate.timer
```

## Notes

This project intentionally does not include a live `mods.json`, `state.json`, webhooks, or local backups. Keep your real `mods.json` private if it contains a Discord webhook.
