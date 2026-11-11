import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "channels.json"
OUTPUT = ROOT / "playlist.m3u"
TIMEOUT = 30


def download(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "My-IPTV/1.0"}
    )
    with urllib.request.urlopen(
        request, timeout=TIMEOUT
    ) as response:
        return response.read().decode("utf-8-sig")


def parse_m3u(content):
    channels = []
    info = None

    for line in content.splitlines():
        line = line.strip()

        if line.startswith("#EXTINF:"):
            info = line

        elif line and not line.startswith("#") and info:
            if line.startswith(("http://", "https://")):
                channels.append((info, line))
            info = None

    return channels


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    channels = []
    seen = set()
    successful_sources = 0

    for source in config.get("sources", []):
        if not source.get("enabled", False):
            continue

        try:
            content = download(source["url"])
            found = parse_m3u(content)

            if not found:
                raise ValueError("No channels found")

            successful_sources += 1
            print(source["name"], len(found), "channels")

            for info, url in found:
                if url not in seen:
                    seen.add(url)
                    channels.append((info, url))

        except Exception as error:
            print("Source error:", source["name"], error)

    for channel in config.get("channels", []):
        url = channel.get("url", "")
        if not url.startswith(("http://", "https://")):
            continue

        if url in seen:
            continue

        seen.add(url)
        name = channel.get("name", "Unknown")
        group = channel.get("group", "Favorites")
        logo = channel.get("logo", "")
        language = channel.get("language", "")

        for value in (name, group, logo, language):
            if "\n" in value or "\r" in value:
                raise ValueError("Invalid channel metadata")

        group = re.sub(r'"', "'", group)
        logo = re.sub(r'"', "'", logo)
        language = re.sub(r'"', "'", language)

        info = (
            f'#EXTINF:-1 tvg-logo="{logo}" '
            f'tvg-language="{language}" '
            f'group-title="{group}",{name}'
        )
        channels.append((info, url))

    if not channels:
        raise RuntimeError(
            "No channels available; keeping old playlist"
        )

    if not successful_sources:
        print("No source downloaded successfully")

    lines = ["#EXTM3U"]
    for info, url in channels:
        lines.extend((info, url))

    new_content = "\n".join(lines) + "\n"

    if OUTPUT.exists():
        old_content = OUTPUT.read_text(encoding="utf-8")
        if old_content == new_content:
            print("Playlist unchanged")
            return

    OUTPUT.write_text(new_content, encoding="utf-8")
    print("Playlist updated:", len(channels), "channels")


if __name__ == "__main__":
    main()
