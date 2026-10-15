"""Vendor d810-ng's MBA rule DSL and rules (AGPL-3.0) into binja_d810/third_party/d810.

Usage: uv run python scripts/vendor_d810.py
"""

import re
import ssl
import urllib.request
from pathlib import Path

SYSTEM_CA = Path("/etc/ssl/cert.pem")

REPO = "w00tzenheimer/d810-ng"
SHA = "2e59ab4ce18e5e82911880137d1285d95279981a"
RULE_MODULES = ("__init__", "_base", "add", "and_", "bnot", "cst", "hodur", "misc",
                "mov", "mul", "neg", "or_", "predicates", "sub", "xor")
FILES = [
    "src/d810/core/typing.py", "src/d810/core/registry.py", "src/d810/core/bits.py",
    "src/d810/mba/dsl.py", "src/d810/mba/constraints.py", "src/d810/mba/backends/z3.py",
    *(f"src/d810/mba/rules/{name}.py" for name in RULE_MODULES),
]
DEST = Path(__file__).resolve().parents[1] / "binja_d810" / "third_party" / "d810"
PREFIX = "binja_d810.third_party.d810"
IMPORTS = re.compile(r'(?<=from )d810(?=[ .])|(?<=import )d810(?=[ .])|(?<=")d810(?=\.)')
HEADER = (
    "# Vendored from https://github.com/{repo} @ {sha} ({path}). AGPL-3.0-only.\n"
    "# Do not edit: regenerate with scripts/vendor_d810.py.\n"
)


def _ssl_context() -> ssl.SSLContext:
    # python.org macOS builds have no CA store until "Install Certificates.command" runs;
    # also trust the system bundle so verification works either way.
    context = ssl.create_default_context()
    if SYSTEM_CA.exists():
        context.load_verify_locations(SYSTEM_CA)
    return context


def main() -> None:
    context = _ssl_context()
    for path in FILES:
        url = f"https://raw.githubusercontent.com/{REPO}/{SHA}/{path}"
        text = urllib.request.urlopen(url, context=context).read().decode()
        out = DEST / path.removeprefix("src/d810/")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(HEADER.format(repo=REPO, sha=SHA, path=path) + IMPORTS.sub(PREFIX, text))
        print(f"wrote {out.relative_to(DEST.parents[2])}")


if __name__ == "__main__":
    main()
