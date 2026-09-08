"""Hermetic curl substitute: synthetic metadata only, never asset downloads."""

import json
import os
import sys
from urllib.parse import urlparse


def main() -> None:
    """Emit a scripted HTTP response with curl-compatible status/header output."""
    args = sys.argv[1:]
    if (
        not args
        or args[0] != "-q"
        or any(
            arg.startswith("--location")
            or (arg.startswith("-") and not arg.startswith("--") and "L" in arg)
            for arg in args
        )
    ):
        raise RuntimeError("Unexpected ambient configuration or redirect permission")
    urls = [arg for arg in args if arg.startswith("https://")]
    if len(urls) != 1 or "-o" in args:
        raise RuntimeError("Unexpected download or request")
    authorization = None
    for index, arg in enumerate(args):
        if arg == "-H" and args[index + 1].startswith("Authorization: "):
            authorization = args[index + 1].removeprefix("Authorization: ")
    if authorization and authorization != "token " + os.environ["EXPECTED_TOKEN"]:
        raise RuntimeError("Unexpected credential selection")
    responses = json.loads(os.environ["HTTP_RESPONSES"])
    response = responses[0 if authorization else -1]
    print(
        "REQUEST " + json.dumps({"url": urls[0], "authenticated": bool(authorization)}),
        file=sys.stderr,
    )
    if urlparse(urls[0]).scheme != "https":
        raise RuntimeError("Unexpected transport")
    if response["status"] == 0:
        sys.exit(7)
    if "-i" in args or "-D" in args:
        if response.get("interim"):
            print("HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r")
        print(f"HTTP/1.1 {response['status']} Synthetic\r")
        for key, value in response.get("headers", {}).items():
            print(f"{key}: {value}\r")
        print("\r")
    print(json.dumps(response["body"], indent=response.get("indent")))
    if "-w" in args:
        print(response["status"])


if __name__ == "__main__":
    main()
