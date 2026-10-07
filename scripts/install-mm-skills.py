#!/usr/bin/env python3
"""Install the repository's mm skills in enabled Mjolnir profile homes."""
import argparse
import os
from pathlib import Path
import subprocess
import tomllib


def install(config, repository):
    data = tomllib.loads(Path(config).read_text())
    installed = []
    for profile in data.get("profiles", {}).values():
        if not profile.get("enabled", True) or not profile.get("home"):
            continue
        root = Path(profile["home"]).expanduser() / "skills"
        root.mkdir(parents=True, exist_ok=True)
        for source in sorted((Path(repository) / "skills").glob("mm-*")):
            target = root / source.name
            if target.is_symlink() and target.resolve() == source.resolve():
                continue
            if target.exists() or target.is_symlink():
                raise ValueError(f"refusing to overwrite an existing skill: {target}")
            target.symlink_to(source.resolve(), target_is_directory=True)
            installed.append(str(target))
    return installed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/mjolnir/config.toml")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--service-listen", help="install/start the user service on this private host IP")
    args = parser.parse_args()
    for path in install(args.config, args.repository):
        print(path)
    if args.service_listen:
        import ipaddress
        address = ipaddress.ip_address(args.service_listen)
        if not address.is_private or address.is_unspecified:
            raise ValueError("service must listen on a specific private address")
        repo = args.repository.resolve()
        if any(c in str(repo) for c in ['\n', '"', '%']):
            raise ValueError("unsupported service repository path")
        unit = Path.home() / ".config/systemd/user/mm-skills.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f'''[Unit]
Description=MergeMarshall batch skill service
After=network-online.target

[Service]
Type=simple
WorkingDirectory="{repo}"
ExecStart=/usr/bin/python3 "{repo}/mm_service.py" --listen {address}
Environment=PATH=%h/.cargo/bin:%h/.local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
''')
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], env=env, check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "mm-skills.service"], env=env, check=True)
        subprocess.run(["systemctl", "--user", "restart", "mm-skills.service"], env=env, check=True)
