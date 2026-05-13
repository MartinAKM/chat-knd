import re

# Tune these if too many / too few chunks pass through
MIN_CHUNK_CHARS = 60       # chunks shorter than this are dropped
MIN_ALPHA_RATIO = 0.30     # at least 30 % of chars must be alphanumeric

# ROTINA block: from "ROTINA:" label up to and including "SENHA:" label.
# This block contains only ERP menu navigation paths and empty credentials —
# never useful as RAG context.
_ROTINA_START = re.compile(r"^[ \t]*ROTINA[ \t]*:", re.IGNORECASE | re.MULTILINE)
_ROTINA_END   = re.compile(r"^[ \t]*SENHA[ \t]*:.*$",  re.IGNORECASE | re.MULTILINE)
_ROTINA_LOOKAHEAD = 30   # max lines to scan for SENHA: after ROTINA:


def strip_rotina_block(text: str) -> str:
    """Remove every ROTINA…SENHA access-metadata block from the text."""
    lines = text.splitlines(keepends=True)
    result = []
    i = 0
    while i < len(lines):
        if _ROTINA_START.match(lines[i]):
            # Scan ahead for the closing SENHA: line
            end = None
            for j in range(i, min(i + _ROTINA_LOOKAHEAD, len(lines))):
                if _ROTINA_END.match(lines[j]):
                    end = j
                    break
            if end is not None:
                # Skip ROTINA block + trailing blank lines
                i = end + 1
                while i < len(lines) and lines[i].strip() == "":
                    i += 1
            else:
                # SENHA: not found nearby — keep the line unchanged
                result.append(lines[i])
                i += 1
        else:
            result.append(lines[i])
            i += 1
    return "".join(result)


def clean_text(text: str) -> str:
    """Normalise raw extracted text before it is chunked."""

    # 1. Remove control characters (keep \n and space; drop \t, \r, form-feed, etc.)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    # 2. Tabs and carriage returns → single space
    text = text.replace("\t", " ").replace("\r", " ")

    # 3. Process line by line
    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        # 3a. Collapse runs of 2+ spaces to one
        line = re.sub(r" {2,}", " ", line)
        # 3b. Strip leading/trailing whitespace
        line = line.strip()
        # 3c. Drop lines with no alphanumeric content (page numbers, dividers,
        #     underscores, dashes, dots that PDFs often produce)
        if line and not re.search(r"[A-Za-z0-9À-ÿ]", line):
            continue
        cleaned_lines.append(line)

    # 4. Collapse runs of 3+ consecutive blank lines to a single blank line
    text = _collapse_blank_lines(cleaned_lines)

    return text.strip()


def is_good_chunk(chunk: str) -> bool:
    """Return False for chunks that carry little real information."""
    stripped = chunk.strip()
    if len(stripped) < MIN_CHUNK_CHARS:
        return False
    alphanumeric = sum(1 for c in stripped if c.isalnum())
    return alphanumeric / len(stripped) >= MIN_ALPHA_RATIO


def _collapse_blank_lines(lines: list[str]) -> str:
    result = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:          # allow at most one consecutive blank line
                result.append(line)
        else:
            blank_run = 0
            result.append(line)
    return "\n".join(result)
