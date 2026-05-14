import re

# Tune these if too many / too few chunks pass through
MIN_CHUNK_CHARS = 60       # chunks shorter than this are dropped
MIN_ALPHA_RATIO = 0.30     # at least 30 % of chars must be alphanumeric

_TERMINAL = re.compile(r"[.!?:;]\s*$")

# Matches any line that contains a greeting/pleasantry keyword.
# Used together with a word-count cap so lines with real content are kept.
_GREETING_KW = re.compile(
    r"\b(?:"
    # Portuguese — time-of-day greetings
    r"bom\s+dia|boa\s+tarde|boa\s+noite|boa\s+manh[aã]"
    # Portuguese — general salutations / closings
    r"|ol[aá]|oii*"
    r"|tudo\s+bem|tudo\s+bom|como\s+vai|como\s+est[aá]"
    r"|obrigad[ao]s?|muito\s+obrigad[ao]s?"
    r"|desde\s+j[aá]"
    r"|att\.?|atenciosamente"
    r"|abra[cç]os?"
    r"|at[eé]\s+(?:logo|mais|breve)"
    r"|tchau|adeus"
    r"|aguardo(?:\s+retorno)?|aguardamos"
    r"|por\s+gentileza|por\s+favor"
    # Spanish
    r"|hola|buenos?\s+d[ií]as?|buenas?\s*(?:tardes?|noches?)?"
    r"|gracias|muchas\s+gracias|de\s+nada"
    r"|saludos?|hasta\s+luego|adi[oó]s"
    # English
    r"|hello|hi+|hey+"
    r"|good\s+(?:morning|afternoon|evening|day)"
    r"|thanks?(?:\s+you)?|thank\s+you"
    r"|regards?|best\s+regards?|kind\s+regards?"
    r"|bye+|goodbye"
    r")\b",
    re.IGNORECASE,
)

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

    # 4. Re-join lines broken at every word (common in some PDFs that store
    #    text as individually positioned glyphs rather than as a text flow).
    #    Only triggers on runs of ≥2 consecutive short lines so isolated
    #    one-word headings are left untouched.
    cleaned_lines = _rejoin_word_fragments(cleaned_lines)

    # 5. Collapse runs of 3+ consecutive blank lines to a single blank line
    text = _collapse_blank_lines(cleaned_lines)

    return text.strip()


def is_good_chunk(chunk: str) -> bool:
    """Return False for chunks that carry little real information."""
    stripped = chunk.strip()
    if len(stripped) < MIN_CHUNK_CHARS:
        return False
    alphanumeric = sum(1 for c in stripped if c.isalnum())
    return alphanumeric / len(stripped) >= MIN_ALPHA_RATIO


def strip_greetings(text: str) -> str:
    """
    Remove lines that consist purely of greetings / pleasantries.

    A line is dropped when it has at most 6 words AND contains at least one
    greeting keyword.  Lines that start with a greeting but continue with
    real content (e.g. "Bom dia, o sistema está retornando erro 404") are kept
    because the word count exceeds the cap.
    """
    result = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and len(stripped.split()) <= 6 and _GREETING_KW.search(stripped):
            continue
        result.append(line)
    return "\n".join(result)


def _rejoin_word_fragments(lines: list[str]) -> list[str]:
    """
    Fix PDFs that store text as individually-positioned words, causing pypdf to
    emit one word per line with space-only lines in between.

    Strategy: when a line is short (≤ 3 words) and the last accumulated
    non-blank line does not yet end a sentence, merge the short line into that
    previous line and discard the blank separators between them.  Blank lines
    that follow a sentence-ending line are kept, preserving real paragraph
    boundaries.
    """
    out = []

    for line in lines:
        if not line:
            out.append(line)
            continue

        last_idx = next((j for j in range(len(out) - 1, -1, -1) if out[j]), None)

        should_merge = (
            last_idx is not None
            and len(line.split()) <= 3
            and not _TERMINAL.search(out[last_idx])
        )

        if should_merge:
            out[last_idx] += " " + line
            del out[last_idx + 1:]   # drop blank word-separators between the two
        else:
            out.append(line)

    return out


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
