"""binja-d810: d810-style MBA deobfuscation for Binary Ninja."""

try:
    import binaryninja
except ImportError:  # plain pytest, outside Binary Ninja
    binaryninja = None
