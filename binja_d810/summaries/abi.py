"""ABI facts used only to bound stack scratch storage, never to infer register arguments.

System V AMD64: https://gitlab.com/x86-psABIs/x86-64-ABI/-/blob/master/x86-64-ABI/low-level-sys-info.tex
Apple ARM64: https://developer.apple.com/documentation/xcode/writing-arm64-code-for-apple-platforms
Unknown and kernel platforms get no red zone.
"""

_RED_ZONES = {("linux-x86_64", "x86_64"): 128, ("mac-x86_64", "x86_64"): 128,
              ("mac-aarch64", "aarch64"): 128, ("ios-aarch64", "aarch64"): 128}


def red_zone(platform, architecture, cap):
    return min(max(0, cap), _RED_ZONES.get((platform, architecture), 0))
