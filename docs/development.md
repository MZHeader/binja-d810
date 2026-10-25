# Development

[Back to README](../README.md)

Run these commands from the repository root.

## Tests

```sh
uv sync && uv run pytest    # unit tests, no Binary Ninja needed
tests/samples/build.sh      # build the sample binaries (needs Homebrew LLVM for PE/ELF)
scripts/binja_pytest.sh     # integration tests, in a hidden Binary Ninja instance
```

The integration tests need `pytest` installed in Binary Ninja's Python too.

## License

AGPL-3.0, since the rules in `binja_d810/third_party/d810/` come from d810-ng (re-download them
with `scripts/vendor_d810.py`). CoBRA is Apache-2.0 and isn't included.
