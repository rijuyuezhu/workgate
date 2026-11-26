# Offline o200k_base tokenizer

Imported from `fwerkor/local-shell-mcp` v6.0.3 (`f0ecf8f8039345f24d0de4402983766787f4574e`).

- `webui/tokenizer.py`: `70f937d0c5bbd16b040cc8c3fa0409d359253298`
- `webui/encodings/o200k_base.tiktoken.gz`: `439d78229e656170cc2cc46c7313d37f9854828b` (unchanged)
- `tests/test_webui_tokenizer_parity.py`: `7d4c4927afee26bb26f185bdf66105171925e232`
- `tests/fixtures/librechat/tokenizer_vectors.json`: `f13658aed71150276f726ac496c522d87a9c9651` (unchanged)

Retains the pinned LibreChat-derived UTF-16 bounded chunk algorithm, offline rank integrity check, and conservative UTF-8 byte count fallback. The fallback is used when the optional tokenizer is unavailable, notably Windows ARM64. No runtime download. Exact tokenizer parity is qualified in the optional-extra CI environments.
