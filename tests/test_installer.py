import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).parents[1] / "install.sh"


def normalize(kind: str, value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), f"--normalize-{kind}", value],
        capture_output=True,
        check=False,
        text=True,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@channel_name", "@channel_name"),
        ("channel_name", "@channel_name"),
        ("https://t.me/channel_name", "@channel_name"),
        ("https://t.me/channel_name/123?single", "@channel_name"),
        ("https://t.me/s/channel_name/123", "@channel_name"),
        ("https://telegram.me/channel_name", "@channel_name"),
        ("tg://resolve?domain=channel_name&post=123", "@channel_name"),
        ("-1001234567890", "-1001234567890"),
    ],
)
def test_channel_formats(raw: str, expected: str) -> None:
    result = normalize("channel", raw)
    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize(
    "link",
    ["https://t.me/+privateInvite", "https://t.me/joinchat/private", "https://t.me/c/123/4"],
)
def test_private_invite_link_is_rejected(link: str) -> None:
    assert normalize("channel", link).returncode == 2


def test_domain_accepts_url_or_hostname() -> None:
    assert normalize("domain", "https://Bot.Example.com/").stdout == "bot.example.com"
    assert normalize("domain", "bot.example.com").stdout == "bot.example.com"
