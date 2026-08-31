#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

API_URL_TEMPLATE = "https://{community}.thunderstore.io/api/v1/package/"

ALLOWED_DIRS = {"BepInEx", "config", "patchers"}
ENABLE_ROOT_PLUGINS_FALLBACK = True
ENABLE_SINGLE_DLL_FALLBACK = True

LOOSE_PAYLOAD_EXTS = {".dll", ".pdb", ".xml"}
EXIT_UPDATES_AVAILABLE = 10


@dataclass(frozen=True)
class ModId:
    author: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.author}-{self.name}"


@dataclass
class ModVersion:
    mod: ModId
    version: str
    download_url: str
    dependencies: List[str]


def http_get_json(url: str, timeout: int = 60) -> object:
    req = Request(url, headers={"User-Agent": "valheim-modupdater/2.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def post_discord_webhook_json(webhook_url: str, payload: dict, timeout: int = 20) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "valheim-modupdater/2.0"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        resp.read()


def clamp(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 12] + "\n…(truncated)"


def parse_mod_key(s: str) -> ModId:
    if "-" not in s:
        raise ValueError(f"Invalid mod id '{s}'. Expected 'Author-PackageName'.")
    author, name = s.split("-", 1)
    author = author.strip()
    name = name.strip()
    if not author or not name:
        raise ValueError(f"Invalid mod id '{s}'.")
    return ModId(author=author, name=name)


def parse_dependency(dep: str) -> Tuple[ModId, str]:
    parts = dep.split("-")
    if len(parts) < 3:
        raise ValueError(f"Invalid dependency string '{dep}'.")
    version = parts[-1]
    author = parts[0]
    name = "-".join(parts[1:-1])
    return ModId(author=author, name=name), version


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def unzip_to_dir(zip_path: Path, dest_dir: Path) -> None:
    safe_mkdir(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest_dir)


def copy_tree_merge(src: Path, dst: Path) -> None:
    safe_mkdir(dst)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        safe_mkdir(target_root)
        for d in dirs:
            safe_mkdir(target_root / d)
        for f in files:
            s = Path(root) / f
            t = target_root / f
            safe_mkdir(t.parent)
            shutil.copy2(s, t)


def load_state(state_path: Path) -> Dict[str, str]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state_path: Path, state: Dict[str, str]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _owner_to_string(owner_field: Any) -> Optional[str]:
    if owner_field is None:
        return None
    if isinstance(owner_field, str):
        return owner_field.strip() or None
    if isinstance(owner_field, dict):
        for k in ("name", "slug", "username"):
            v = owner_field.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def build_index(api_payload: object) -> Dict[str, dict]:
    if not isinstance(api_payload, list):
        raise RuntimeError("Unexpected API payload (expected list).")
    index: Dict[str, dict] = {}
    for pkg in api_payload:
        if not isinstance(pkg, dict):
            continue
        owner = (
            _owner_to_string(pkg.get("owner"))
            or _owner_to_string(pkg.get("namespace"))
            or _owner_to_string(pkg.get("author"))
        )
        name = pkg.get("name")
        if isinstance(name, str):
            name = name.strip()
        else:
            name = None
        if not owner or not name:
            continue
        index[f"{owner}-{name}"] = pkg
    return index


def choose_version(pkg_obj: dict, desired: ModId, mode: str, pinned: Dict[str, str]) -> ModVersion:
    versions = pkg_obj.get("versions", [])
    if not versions:
        raise RuntimeError(f"No versions found for {desired.key}")

    want_ver: Optional[str] = None
    if mode == "pinned":
        want_ver = pinned.get(desired.key)
        if not want_ver:
            raise RuntimeError(f"Mode is pinned but no pinned version provided for {desired.key}")

    selected = None
    if want_ver:
        for v in versions:
            if v.get("version_number") == want_ver:
                selected = v
                break
        if not selected:
            raise RuntimeError(f"Pinned version {want_ver} not found for {desired.key}")
    else:
        selected = versions[0]

    dl = selected.get("download_url")
    ver = selected.get("version_number")
    deps = selected.get("dependencies", []) or []
    if not dl or not ver:
        raise RuntimeError(f"Missing download_url/version_number for {desired.key}")

    return ModVersion(mod=desired, version=ver, download_url=dl, dependencies=list(deps))


def download_file(url: str, dest: Path, timeout: int = 120) -> None:
    req = Request(url, headers={"User-Agent": "valheim-modupdater/2.0"})
    with urlopen(req, timeout=timeout) as resp:
        safe_mkdir(dest.parent)
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def resolve_all(index: Dict[str, dict], roots: List[ModId], mode: str, pinned: Dict[str, str]) -> List[ModVersion]:
    resolved: Dict[str, ModVersion] = {}
    visiting: Set[str] = set()

    def dfs(mod: ModId) -> None:
        key = mod.key
        if key in resolved:
            return
        if key in visiting:
            raise RuntimeError(f"Dependency cycle detected at {key}")
        visiting.add(key)

        pkg = index.get(key)
        if not pkg:
            raise RuntimeError(f"Mod not found on Thunderstore: {key}")

        mv = choose_version(pkg, mod, mode, pinned)

        for dep in mv.dependencies:
            dep_id, dep_ver = parse_dependency(dep)
            if mode == "pinned" and dep_id.key not in pinned:
                pinned[dep_id.key] = dep_ver
            dfs(dep_id)

        resolved[key] = mv
        visiting.remove(key)

    for r in roots:
        dfs(r)

    return sorted(resolved.values(), key=lambda x: x.mod.key.lower())


def _find_shallowest_dir(root: Path, dirname: str) -> Optional[Path]:
    candidates: List[Path] = []
    for p in root.rglob(dirname):
        if p.is_dir() and p.name == dirname:
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: len(p.relative_to(root).parts))
    return candidates[0]


def _collect_loose_payload_files(root: Path) -> List[Path]:
    bepinex = _find_shallowest_dir(root, "BepInEx")
    config = _find_shallowest_dir(root, "config")
    excluded = [p for p in (bepinex, config) if p]

    out: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        skip = False
        for ex in excluded:
            try:
                p.relative_to(ex)  # type: ignore[arg-type]
                skip = True
                break
            except ValueError:
                pass
        if skip:
            continue
        if p.suffix.lower() in LOOSE_PAYLOAD_EXTS:
            out.append(p)
    return out


def apply_extracted_payload(extracted_root: Path, target_root: Path) -> List[str]:
    applied: List[str] = []
    found = {d: _find_shallowest_dir(extracted_root, d) for d in ALLOWED_DIRS}

    for dname in ("BepInEx", "config"):
        src = found.get(dname)
        if src and src.is_dir():
            dst = target_root / dname
            copy_tree_merge(src, dst)
            applied.append(f"{dname} (from {src.relative_to(extracted_root)})")

    patchers_src = found.get("patchers")
    if patchers_src and patchers_src.is_dir():
        dst = target_root / "BepInEx" / "patchers"
        copy_tree_merge(patchers_src, dst)
        applied.append(f"patchers -> BepInEx/patchers (from {patchers_src.relative_to(extracted_root)})")

    if ENABLE_ROOT_PLUGINS_FALLBACK and not found.get("BepInEx"):
        plugins_src = _find_shallowest_dir(extracted_root, "plugins")
        if plugins_src and plugins_src.is_dir():
            dst = target_root / "BepInEx" / "plugins"
            copy_tree_merge(plugins_src, dst)
            applied.append("plugins -> BepInEx/plugins")
            return applied

    if ENABLE_SINGLE_DLL_FALLBACK and not found.get("BepInEx") and not found.get("config"):
        payload = _collect_loose_payload_files(extracted_root)
        dlls = [p for p in payload if p.suffix.lower() == ".dll"]
        if not dlls:
            return applied
        if len(dlls) > 10:
            return applied

        dst_dir = target_root / "BepInEx" / "plugins"
        safe_mkdir(dst_dir)
        for p in payload:
            shutil.copy2(p, dst_dir / p.name)
        applied.append("loose payload -> BepInEx/plugins")
        return applied

    return applied


def get_webhook_url(cfg: dict) -> Optional[str]:
    env = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if env:
        return env
    val = (cfg.get("discord_webhook") or "").strip()
    return val or None


def build_plan(cfg: dict, state: Dict[str, str]) -> Tuple[List[ModVersion], List[Tuple[str, str, str]], List[Tuple[str, str, str]]]:
    community = cfg.get("community", "valheim")
    mode = cfg.get("mode", "latest").lower()
    if mode not in ("latest", "pinned"):
        raise RuntimeError("mode must be 'latest' or 'pinned'")

    pinned: Dict[str, str] = dict(cfg.get("pinned", {}) or {})
    roots = [parse_mod_key(x) for x in (cfg.get("mods", []) or [])]
    if not roots:
        raise RuntimeError("No mods listed in config.")

    api_url = API_URL_TEMPLATE.format(community=community)
    print(f"[INFO] Fetching Thunderstore index: {api_url}")
    payload = http_get_json(api_url)
    index = build_index(payload)

    mods_resolved = resolve_all(index, roots, mode, pinned)

    planned: List[ModVersion] = []
    transitions_all: List[Tuple[str, str, str]] = []
    transitions_update: List[Tuple[str, str, str]] = []

    for mv in mods_resolved:
        cur = state.get(mv.mod.key, "(unknown)")
        latest = mv.version
        transitions_all.append((mv.mod.key, cur, latest))
        if cur != latest:
            planned.append(mv)
            transitions_update.append((mv.mod.key, cur, latest))

    return planned, transitions_all, transitions_update


def print_check_table(transitions_all: List[Tuple[str, str, str]]) -> None:
    print("\n[CHECK] Versions (current -> latest):")
    for key, cur, latest in transitions_all:
        mark = "OK " if cur == latest else "UPD"
        print(f"  [{mark}] {key}: {cur} -> {latest}")


def embed_updates_found(host: str, restart_unix: int, updates: List[Tuple[str, str, str]]) -> dict:
    # Put updated mods in one embed field; truncate if too long
    lines = [f"`{k}`: **{cur}** → **{new}**" for k, cur, new in updates]
    value = "\n".join(lines) if lines else "None"
    value = clamp(value, 1000)

    return {
        "embeds": [
            {
                "title": "🟡 Valheim updates found",
                "description": f"Server: `{host}`\nRestart scheduled: <t:{restart_unix}:R> (<t:{restart_unix}:T>)",
                "fields": [
                    {"name": "Updates", "value": value, "inline": False},
                ],
            }
        ]
    }


def embed_updates_applied(host: str, updates: List[Tuple[str, str, str]], warnings: List[str]) -> dict:
    lines = [f"`{k}`: **{cur}** → **{new}**" for k, cur, new in updates]
    upd_val = "\n".join(lines) if lines else "None"
    upd_val = clamp(upd_val, 1000)

    fields = [{"name": "Updated mods", "value": upd_val, "inline": False}]
    if warnings:
        warn_val = clamp("\n".join(f"- {w}" for w in warnings[:10]), 1000)
        fields.append({"name": "Warnings", "value": warn_val, "inline": False})

    return {
        "embeds": [
            {
                "title": "✅ Valheim mods updated",
                "description": f"Server: `{host}`",
                "fields": fields,
            }
        ]
    }


def notify_scheduled(cfg: dict, restart_unix: int, transitions_update: List[Tuple[str, str, str]]) -> None:
    webhook = get_webhook_url(cfg)
    if not webhook:
        return
    host = os.uname().nodename
    payload = embed_updates_found(host, restart_unix, transitions_update)
    try:
        post_discord_webhook_json(webhook, payload)
    except (HTTPError, URLError) as e:
        print(f"[WARN] Discord webhook (scheduled) failed: {e}", file=sys.stderr)


def notify_applied(cfg: dict, transitions_update: List[Tuple[str, str, str]], warnings: List[str]) -> None:
    webhook = get_webhook_url(cfg)
    if not webhook:
        return
    host = os.uname().nodename
    payload = embed_updates_applied(host, transitions_update, warnings)
    try:
        post_discord_webhook_json(webhook, payload)
    except (HTTPError, URLError) as e:
        print(f"[WARN] Discord webhook (update) failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync Valheim Thunderstore mods into a mounted Valheim server folder.")
    ap.add_argument("--config", required=True, help="Path to mods.json")
    ap.add_argument("--target", required=True, help="Path to Valheim server root (folder containing BepInEx/)")
    ap.add_argument("--state", default="state.json", help="State file path (default: ./state.json next to config if relative)")
    ap.add_argument("--dry-run", action="store_true", help="Only print what would be done (no changes)")
    ap.add_argument("--check", action="store_true", help="Only check if updates exist (no downloads, no changes)")
    ap.add_argument("--notify", action="store_true", help="Webhook: post only on successful apply (updated mods)")
    ap.add_argument("--notify-scheduled", type=int, default=0,
                    help="Webhook: if updates exist, post a 'restart scheduled' embed with this UNIX timestamp")
    args = ap.parse_args()

    if args.dry_run and args.check:
        raise RuntimeError("Use only one of --dry-run or --check.")

    config_path = Path(args.config).resolve()
    cfg = json.loads(config_path.read_text(encoding="utf-8"))

    target_root = Path(args.target).resolve()
    if not target_root.exists():
        raise RuntimeError(f"Target path does not exist: {target_root}")

    state_path = Path(args.state)
    if not state_path.is_absolute():
        state_path = (config_path.parent / state_path).resolve()
    state = load_state(state_path)

    planned, transitions_all, transitions_update = build_plan(cfg, state)

    if args.check:
        print_check_table(transitions_all)
        if planned and args.notify_scheduled:
            notify_scheduled(cfg, args.notify_scheduled, transitions_update)
        return EXIT_UPDATES_AVAILABLE if planned else 0

    if not planned:
        print("[OK] Nothing to update.")
        return 0

    if args.dry_run:
        print("\n[DRY-RUN] Exiting without changes.")
        return 0

    print("\n[PLAN] Will install/update:")
    for key, prev, new in transitions_update:
        print(f"  - {key}: {prev} -> {new}")

    warnings: List[str] = []
    with tempfile.TemporaryDirectory(prefix="valheim-modupdater-") as td:
        tmp = Path(td)
        downloads = tmp / "downloads"
        extracted = tmp / "extracted"
        safe_mkdir(downloads)
        safe_mkdir(extracted)

        for mv in planned:
            zip_path = downloads / f"{mv.mod.key}-{mv.version}.zip"
            print(f"\n[DL] {mv.mod.key} {mv.version}")
            print(f"     {mv.download_url}")
            download_file(mv.download_url, zip_path)

            out_dir = extracted / f"{mv.mod.key}-{mv.version}"
            unzip_to_dir(zip_path, out_dir)

            applied = apply_extracted_payload(out_dir, target_root)
            if not applied:
                msg = f"{mv.mod.key}: extracted but nothing matched deploy rules."
                warnings.append(msg)
                print(f"[WARN] {msg}", file=sys.stderr)
            else:
                print(f"[APPLY] {mv.mod.key}: deployed {', '.join(applied)}")

            state[mv.mod.key] = mv.version

    save_state(state_path, state)
    print(f"\n[OK] Updated {len(planned)} mod(s). State saved to: {state_path}")

    if args.notify:
        notify_applied(cfg, transitions_update, warnings)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise SystemExit(2)
