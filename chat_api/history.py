import json
import os
import uuid
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_HISTORY_DIR = Path(os.getenv("CHAT_HISTORY_DIR", str(_ROOT / "chat_history")))


def _user_dir(user_id: str) -> Path:
    d = _HISTORY_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(conv_id: str, user_id: str) -> Path:
    return _user_dir(user_id) / f"{conv_id}.json"


def create_conversation(first_message: str, user_id: str) -> str:
    """Create a new conversation file and return its ID."""
    conv_id = str(uuid.uuid4())
    title = first_message.strip()[:60] + ("…" if len(first_message.strip()) > 60 else "")
    now = datetime.now().isoformat()
    _path(conv_id, user_id).write_text(
        json.dumps(
            {"id": conv_id, "title": title, "created_at": now, "updated_at": now, "messages": []},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return conv_id


def append_exchange(conv_id: str, user_id: str, question: str, answer: str, sources: list[str]) -> None:
    """Append a user/assistant exchange to an existing conversation file."""
    path = _path(conv_id, user_id)
    if not path.exists():
        return
    conv = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now().isoformat()
    conv["messages"].append({"role": "user",      "content": question, "timestamp": now})
    conv["messages"].append({"role": "assistant", "content": answer, "sources": sources, "timestamp": now})
    conv["updated_at"] = now
    path.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")


def list_conversations(user_id: str) -> list[dict]:
    """Return summary dicts for all conversations of a user, sorted by updated_at descending."""
    user_dir = _user_dir(user_id)
    convs = []
    for f in user_dir.glob("*.json"):
        try:
            conv = json.loads(f.read_text(encoding="utf-8"))
            convs.append({
                "id":            conv["id"],
                "title":         conv["title"],
                "created_at":    conv["created_at"],
                "updated_at":    conv["updated_at"],
                "message_count": len(conv["messages"]),
            })
        except Exception:
            continue
    return sorted(convs, key=lambda x: x["updated_at"], reverse=True)


def get_conversation(conv_id: str, user_id: str) -> dict | None:
    """Return full conversation data, or None if not found."""
    path = _path(conv_id, user_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_conversation(conv_id: str, user_id: str) -> bool:
    """Delete a conversation file. Returns True if it existed."""
    path = _path(conv_id, user_id)
    if path.exists():
        path.unlink()
        return True
    return False
