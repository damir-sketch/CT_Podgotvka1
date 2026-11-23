"""Проверка getMe без вывода токена — для сервера (Railway) и локально.

Токен берётся из переменной окружения BOT_TOKEN (на Railway — Variables).
Локально, если переменной нет, читается BOT_TOKEN из .env.
В вывод попадают только HTTP-статус или тип ошибки: текст исключений
не печатается, потому что в нём может оказаться URL с токеном.

Запуск:  python check_connection.py      (код выхода 0 — getMe ответил 200)
"""
import os
import sys
from pathlib import Path

import httpx

BASE = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def load_token() -> tuple[str, str]:
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if token:
        return token, "переменная окружения"
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        from dotenv import dotenv_values
        token = (dotenv_values(env_file).get("BOT_TOKEN") or "").strip()
        if token:
            return token, ".env"
    return "", "не найден"


def probe(label: str, method: str, url: str) -> int | None:
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=True) as client:
            status = client.request(method, url).status_code
        print(f"[check] {label}: HTTP {status}", flush=True)
        return status
    except Exception as e:  # noqa: BLE001 — только тип ошибки
        print(f"[check] {label}: {type(e).__name__}", flush=True)
        return None


def main() -> int:
    token, source = load_token()
    print(f"[check] BOT_TOKEN: {source}", flush=True)
    probe("api.telegram.org без токена", "GET", BASE + "/")
    if not token:
        return 2
    status = probe("getMe", "POST", f"{BASE}/bot{token}/getMe")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
