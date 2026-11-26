"""UTF-16 model-only result compaction from pinned upstream approval_runtime.py."""


def truncate_approval_result(content: str, max_chars: int) -> str:
    """Port SDK truncateToolResultContent, including UTF-16-safe boundaries."""
    max_chars = max(0, max_chars)
    encoded = content.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) // 2 <= max_chars:
        return content
    units = "".join(
        chr(int.from_bytes(encoded[index : index + 2], "little"))
        for index in range(0, len(encoded), 2)
    )

    def sliced(start: int, end: int | None = None) -> str:
        value = units[start:end]
        if value and 0xDC00 <= ord(value[0]) <= 0xDFFF:
            value = value[1:]
        if value and 0xD800 <= ord(value[-1]) <= 0xDBFF:
            value = value[:-1]
        return b"".join(
            ord(unit).to_bytes(2, "little") for unit in value
        ).decode("utf-16-le", errors="surrogatepass")

    indicator = f"\n\n… [truncated: {len(units)} chars exceeded {max_chars} limit] …\n\n"
    available = max_chars - len(indicator)
    if available <= 0:
        return sliced(0, max_chars)
    if available < 200:
        return sliced(0, available) + indicator.rstrip()
    # Math.ceil(available * .7), expressed with integer arithmetic.
    head_size = (available * 7 + 9) // 10
    tail_size = available - head_size
    head_end = head_size
    head_newline = units.rfind("\n", 0, head_size + 1)
    if head_newline > head_size - 200 and head_newline > 0:
        head_end = head_newline
    tail_start = len(units) - tail_size
    tail_newline = units.find("\n", tail_start)
    if tail_newline > 0 and tail_newline < tail_start + 200:
        tail_start = tail_newline + 1
    return sliced(0, head_end) + indicator + sliced(tail_start)
