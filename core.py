import io
import os
import re
import random
import sys
import json
import time
import shutil
import logging
import argparse
import threading
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yt_dlp
from ytmusicapi import YTMusic, OAuthCredentials
from PIL import Image, ImageOps

# =========================================================
# НАСТРОЙКИ
# =========================================================
YADISK_API_BASE = "https://cloud-api.yandex.net/v1/disk"
YADISK_ROOT_DIR = "disk:/NikridRadio"
YADISK_TOKEN_PATH = Path("yadisk_token.txt")

PLAYLIST_JSON_PATH = Path("playlist.json")
COVERS_DIR = Path("covers")
TEMP_AUDIO_DIR = Path("temp_audio")
FAILED_LOG_PATH = Path("failed_tracks.log")
UNAVAILABLE_IDS_PATH = Path("unavailable_ids.json")
SYNC_CONFIG_PATH = Path("sync_config.json")  # {"token": "ghp_...", "gistId": "..."}

BROWSER_JSON_PATH = Path("browser.json")
CREDENTIALS_JSON_PATH = Path("credentials.json")
CLIENT_SECRET_PATH = Path("client_secret.json")

WAVE_TARGET_COUNT = 200
COVER_SIZE = (512, 512)
COVER_WEBP_QUALITY = 84
MAX_WORKERS = 5          # Параллельные потоки для скачивания и заливки аудио
COVER_WORKERS = 8        # Параллельные потоки для обложек

# ANSI-цвета для консоли (с автоматическим включением VT100 в Windows 10+)
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[90m"
_ANSI_GREEN = "\033[92m"
_ANSI_YELLOW = "\033[93m"
_ANSI_RED = "\033[91m"
_ANSI_CYAN = "\033[96m"
_ANSI_BOLD = "\033[1m"


def _enable_windows_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
                return True
        except Exception:
            return False
        return False
    return True


_USE_ANSI = _enable_windows_ansi()
_LAST_INLINE_LEN = 0
_UI_LOCK = threading.Lock()


def _c(text: str, code: str) -> str:
    if not _USE_ANSI:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _write_file_log(level: str, msg: str, step: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    step_tag = f" [{step}]" if step else ""
    line = f"{ts} [{level}]{step_tag} {msg}\n"
    with _UI_LOCK:
        try:
            with open(FAILED_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def _clear_inline_unlocked():
    global _LAST_INLINE_LEN
    if _LAST_INLINE_LEN > 0:
        if _USE_ANSI:
            sys.stdout.write(f"\r{' ' * _LAST_INLINE_LEN}\r")
        else:
            sys.stdout.write("\n")
        sys.stdout.flush()
        _LAST_INLINE_LEN = 0


def ui_clear_inline():
    with _UI_LOCK:
        _clear_inline_unlocked()


def ui_status(icon: str, text: str, color: str = "", step: str = "", log_level: str | None = None):
    global _LAST_INLINE_LEN
    clean_msg = f"{icon} {text}".strip()
    colored_msg = _c(clean_msg, color) if color else clean_msg
    with _UI_LOCK:
        if _USE_ANSI and sys.stdout.isatty():
            pad = max(0, _LAST_INLINE_LEN - len(clean_msg))
            sys.stdout.write(f"\r{colored_msg}{' ' * pad}")
            sys.stdout.flush()
            _LAST_INLINE_LEN = len(clean_msg)
        else:
            print(clean_msg, flush=True)
            _LAST_INLINE_LEN = 0
    if log_level:
        _write_file_log(log_level, text, step=step)


def ui_line(icon: str, text: str, color: str = "", step: str = "", log_level: str | None = None):
    msg = f"{icon} {text}".strip()
    with _UI_LOCK:
        _clear_inline_unlocked()
        print(_c(msg, color) if color else msg, flush=True)
    if log_level:
        _write_file_log(log_level, text, step=step)


def ui_section(title: str):
    with _UI_LOCK:
        _clear_inline_unlocked()
        print(f"\n{_c(f'── {title} ', _ANSI_CYAN + _ANSI_BOLD)}{_c('─' * max(10, 56 - len(title)), _ANSI_DIM)}", flush=True)


def ui_progress(step_label: str, current: int, total: int, detail: str = ""):
    global _LAST_INLINE_LEN
    total = max(total, 1)
    pct = min(100, int((current / total) * 100))
    bar_len = 20
    filled = min(bar_len, int((pct / 100) * bar_len))
    bar_plain = f"[{'#' * filled}{'.' * (bar_len - filled)}]"
    plain = f"  {step_label} {bar_plain} {pct:>3}% ({current}/{total})"
    if detail:
        plain += f" • {detail}"

    with _UI_LOCK:
        if _USE_ANSI and sys.stdout.isatty():
            colored = (
                f"  {_c(step_label, _ANSI_BOLD)} "
                f"{_c('[' + '#' * filled, _ANSI_GREEN)}{_c('.' * (bar_len - filled), _ANSI_DIM)} "
                f"{_c(f'{pct:>3}%', _ANSI_CYAN)} ({current}/{total})"
            )
            if detail:
                colored += f" {_c('• ' + detail, _ANSI_DIM)}"
            pad = max(0, _LAST_INLINE_LEN - len(plain))
            sys.stdout.write(f"\r{colored}{' ' * pad}")
            sys.stdout.flush()
            _LAST_INLINE_LEN = len(plain)
        else:
            if current == total or current == 1 or current % 5 == 0:
                print(plain, flush=True)
                _LAST_INLINE_LEN = 0


def _compact_err(err: Exception | str, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(err)).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


logging.basicConfig(level=logging.ERROR, handlers=[logging.NullHandler()])


# =========================================================
# YANDEX DISK REST API
# =========================================================
def load_yadisk_token() -> str:
    if not YADISK_TOKEN_PATH.exists():
        raise RuntimeError(
            f"Не найден файл {YADISK_TOKEN_PATH}. Создайте его и вставьте туда OAuth-токен Яндекс Диска."
        )
    token = YADISK_TOKEN_PATH.read_text(encoding="utf-8-sig").strip()
    if not token:
        raise RuntimeError(f"Файл {YADISK_TOKEN_PATH} пуст. Вставьте OAuth-токен Яндекс Диска.")
    return token


def yadisk_request(
    method: str,
    endpoint: str,
    token: str,
    params: dict | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 45,
    absolute_url: str | None = None,
) -> tuple[int, dict]:
    if absolute_url:
        url = absolute_url
    else:
        url = f"{YADISK_API_BASE}{endpoint}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

    req_headers = {"User-Agent": "Nikrid-Radio-Sync/2.1"}
    if not absolute_url:
        req_headers["Authorization"] = f"OAuth {token}"
        req_headers["Accept"] = "application/json"
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed


def ensure_yadisk_folder(token: str):
    status, data = yadisk_request(
        "PUT", "/resources", token, params={"path": YADISK_ROOT_DIR}
    )
    if status in (201, 409):
        return
    raise RuntimeError(f"Не удалось создать папку {YADISK_ROOT_DIR} на Яндекс Диске: HTTP {status} {data}")


def fetch_existing_yadisk_files(token: str) -> dict[str, dict]:
    result = {}
    offset = 0
    limit = 200
    while True:
        status, data = yadisk_request(
            "GET",
            "/resources",
            token,
            params={
                "path": YADISK_ROOT_DIR,
                "limit": limit,
                "offset": offset,
                "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.public_url,_embedded.total",
            },
        )
        if status != 200:
            ui_line("!", f"Не удалось получить список файлов Яндекс Диска: HTTP {status}", _ANSI_YELLOW, step="yadisk_list", log_level="WARN")
            break
        embedded = data.get("_embedded") or {}
        items = embedded.get("items") or []
        for item in items:
            name = item.get("name", "")
            stem = Path(name).stem
            if stem:
                result[stem] = {
                    "path": item.get("path", f"{YADISK_ROOT_DIR}/{name}"),
                    "public_url": item.get("public_url", ""),
                }
        total = embedded.get("total", 0)
        offset += len(items)
        if not items or offset >= total:
            break
    return result


def publish_yadisk_resource(token: str, remote_path: str) -> str:
    status, data = yadisk_request(
        "PUT", "/resources/publish", token, params={"path": remote_path}
    )
    if status not in (200, 201, 202):
        raise RuntimeError(f"Ошибка публикации {remote_path}: HTTP {status} {data}")

    for _ in range(5):
        g_status, g_data = yadisk_request(
            "GET",
            "/resources",
            token,
            params={"path": remote_path, "fields": "public_url,path"},
        )
        if g_status == 200 and g_data.get("public_url"):
            return g_data["public_url"]
        time.sleep(0.35)

    raise RuntimeError(f"Не удалось получить public_url после публикации {remote_path}")


def upload_and_publish_yadisk(token: str, local_path: Path, video_id: str) -> tuple[str, str]:
    ext = local_path.suffix.lower().lstrip(".") or "webm"
    remote_path = f"{YADISK_ROOT_DIR}/{video_id}.{ext}"

    status, data = yadisk_request(
        "GET",
        "/resources/upload",
        token,
        params={"path": remote_path, "overwrite": "true"},
    )
    if status != 200 or not data.get("href"):
        raise RuntimeError(f"Не получен upload href для {remote_path}: HTTP {status} {data}")

    file_bytes = local_path.read_bytes()
    put_status, put_data = yadisk_request(
        "PUT",
        "",
        token,
        data=file_bytes,
        headers={"Content-Type": "application/octet-stream"},
        timeout=120,
        absolute_url=data["href"],
    )
    if put_status not in (200, 201, 202):
        raise RuntimeError(f"Ошибка PUT загрузки {remote_path}: HTTP {put_status} {put_data}")

    public_url = publish_yadisk_resource(token, remote_path)
    return remote_path, public_url


def delete_from_yadisk(token: str, remote_path: str):
    if not remote_path:
        return
    status, data = yadisk_request(
        "DELETE",
        "/resources",
        token,
        params={"path": remote_path, "permanently": "true"},
    )
    if status in (202, 204):
        _write_file_log("INFO", f"Удалён с Яндекс Диска: {remote_path}", step="cleanup")
    elif status == 404:
        _write_file_log("INFO", f"Файл уже отсутствует на Яндекс Диске: {remote_path}", step="cleanup")
    else:
        ui_line("!", f"Не удалось удалить {remote_path}: HTTP {status}", _ANSI_YELLOW, step="cleanup", log_level="WARN")


# =========================================================
# ЛОКАЛЬНЫЕ ОБЛОЖКИ (COVERS/<videoId>.webp, 512x512)
# =========================================================
def upgrade_cover_url(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    u = url.strip()
    if "googleusercontent.com" in u or "ggpht.com" in u:
        u = re.sub(r"=w\d+-h\d+[^&?]*", "=w600-h600-l90-rj", u)
        u = re.sub(r"=s\d+[^&?]*", "=s600", u)
    return u


def ensure_local_cover(video_id: str, remote_url: str = "") -> str:
    if not video_id:
        return ""
    COVERS_DIR.mkdir(exist_ok=True)
    out_path = COVERS_DIR / f"{video_id}.webp"
    rel_path = f"covers/{video_id}.webp"

    if out_path.exists() and out_path.stat().st_size > 500:
        return rel_path

    candidates = []
    if remote_url and remote_url.startswith("http"):
        candidates.append(upgrade_cover_url(remote_url))
    candidates.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    candidates.append(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")

    seen = set()
    last_err = ""
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read()
            if len(raw) < 512:
                last_err = f"Слишком маленький ответ ({len(raw)} B) с {url}"
                continue

            with Image.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                w, h = img.size
                if "ytimg.com" in url and "hqdefault" in url and abs((w / max(h, 1)) - (4 / 3)) < 0.05:
                    crop_y = int(h * 0.125)
                    if h - 2 * crop_y >= 100:
                        img = img.crop((0, crop_y, w, h - crop_y))

                img = ImageOps.fit(img, COVER_SIZE, method=Image.Resampling.LANCZOS)
                img.save(out_path, format="WEBP", quality=COVER_WEBP_QUALITY, method=6)
                return rel_path
        except Exception as e:
            last_err = f"{url} -> {_compact_err(e, 100)}"

    _write_file_log("WARN", f"[{video_id}] Обложка не скачана: {last_err}", step="covers")
    return ""


def delete_local_cover(video_id: str):
    if not video_id:
        return
    p = COVERS_DIR / f"{video_id}.webp"
    try:
        if p.exists():
            p.unlink()
    except Exception as e:
        _write_file_log("WARN", f"Не удалось удалить обложку {p}: {_compact_err(e)}", step="cleanup")


# =========================================================
# АВТОРИЗАЦИЯ YOUTUBE MUSIC
# =========================================================
def _extract_client_creds(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "installed" in data:
            data = data["installed"]
        elif "web" in data:
            data = data["web"]
        return data.get("client_id", "").strip(), data.get("client_secret", "").strip()
    except Exception:
        return "", ""


def get_ytmusic_client() -> YTMusic:
    auth_data = {}
    if BROWSER_JSON_PATH.exists():
        try:
            auth_data = json.loads(BROWSER_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    if isinstance(auth_data, dict) and ("access_token" in auth_data or "refresh_token" in auth_data):
        client_id, client_secret = _extract_client_creds(CREDENTIALS_JSON_PATH)
        if not client_id or not client_secret:
            client_id, client_secret = _extract_client_creds(CLIENT_SECRET_PATH)

        if client_id and client_secret:
            return YTMusic(
                str(BROWSER_JSON_PATH),
                oauth_credentials=OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret
                )
            )
        raise RuntimeError(
            "В browser.json лежат OAuth-токены, но client_id/client_secret не найдены в credentials.json или client_secret.json."
        )

    return YTMusic(str(BROWSER_JSON_PATH))


# =========================================================
# ОБЛАЧНАЯ СИНХРОНИЗАЦИЯ БУФЕРА (GITHUB GIST)
# =========================================================
def load_cloud_sync_state() -> dict:
    token = ""
    gist_id = ""
    if SYNC_CONFIG_PATH.exists():
        try:
            cfg = json.loads(SYNC_CONFIG_PATH.read_text(encoding="utf-8"))
            token = cfg.get("token", "").strip()
            gist_id = cfg.get("gistId", "").strip()
        except Exception:
            pass

    empty = {"likeIds": [], "dislikeIds": [], "waveListenedIds": [], "dislikeTitles": []}
    if not token:
        ui_line("!", f"Токен Gist не найден в {SYNC_CONFIG_PATH} — буфер пропущен", _ANSI_YELLOW, step="gist_sync", log_level="WARN")
        return empty

    try:
        if not gist_id:
            list_req = urllib.request.Request(
                "https://api.github.com/gists?per_page=30",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Nikrid-Radio-Sync"
                }
            )
            with urllib.request.urlopen(list_req, timeout=15) as resp:
                gists = json.loads(resp.read().decode("utf-8"))
                for g in gists:
                    if "nikrid_sync.json" in g.get("files", {}):
                        gist_id = g.get("id", "")
                        SYNC_CONFIG_PATH.write_text(
                            json.dumps({"token": token, "gistId": gist_id}, indent=2),
                            encoding="utf-8"
                        )
                        break

        if not gist_id:
            return empty

        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Nikrid-Radio-Sync"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data.get("files", {}).get("nikrid_sync.json", {}).get("content", "{}")
            return json.loads(content)
    except Exception as e:
        ui_line("!", f"Gist не прочитан ({_compact_err(e, 90)}) — продолжаю с локальными данными", _ANSI_YELLOW, step="gist_sync", log_level="WARN")
        return empty


def clear_cloud_sync_buffer(processed_like_ids: set, processed_dislike_ids: set, processed_listened_ids: set):
    if not (processed_like_ids or processed_dislike_ids or processed_listened_ids):
        return
    if not SYNC_CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(SYNC_CONFIG_PATH.read_text(encoding="utf-8"))
        token = cfg.get("token", "").strip()
        gist_id = cfg.get("gistId", "").strip()
        if not token or not gist_id:
            return

        current = load_cloud_sync_state()
        rem_likes = [x for x in current.get("likeIds", []) if x not in processed_like_ids]
        rem_dislikes = [x for x in current.get("dislikeIds", []) if x not in processed_dislike_ids]
        rem_listened = [x for x in current.get("waveListenedIds", []) if x not in processed_listened_ids]
        rem_like_titles = [x for x in current.get("likeTitles", []) if x.get("id") not in processed_like_ids]
        rem_dislike_titles = [x for x in current.get("dislikeTitles", []) if x.get("id") not in processed_dislike_ids]

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "updatedAt": now_iso,
            "clearedAt": now_iso,
            "likeIds": rem_likes,
            "dislikeIds": rem_dislikes,
            "waveListenedIds": rem_listened,
            "likeTitles": rem_like_titles,
            "dislikeTitles": rem_dislike_titles,
        }
        body = json.dumps({"files": {"nikrid_sync.json": {"content": json.dumps(payload, ensure_ascii=False, indent=2)}}}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            data=body,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "Nikrid-Radio-Sync",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                ui_line("✓", "Облачный буфер лайков/дизлайков очищен в Gist", _ANSI_GREEN, step="gist_sync")
    except Exception as e:
        ui_line("!", f"Не удалось очистить буфер в Gist ({_compact_err(e, 90)})", _ANSI_YELLOW, step="gist_sync", log_level="WARN")


def reconcile_like_buffer_in_gist(
    existing_tracks: list[dict],
    ytm_liked_vids: set[str],
    disliked_ids: set,
    processed_listened_ids: set,
    reset_wave: bool = False,
):
    """
    Сверяет лайки на сайте с реальными лайками в YouTube Music:
    - Если трек лайкнут на сайте (liked=True или source='playlist'), но его НЕТ в лайках YTMusic -> помещает в Буфер: Лайк.
    - Если трек УЖЕ есть в лайках YTMusic -> убирает из Буфера: Лайк (перенос завершён).
    """
    if not SYNC_CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(SYNC_CONFIG_PATH.read_text(encoding="utf-8"))
        token = cfg.get("token", "").strip()
        gist_id = cfg.get("gistId", "").strip()
        if not token or not gist_id:
            return

        current = load_cloud_sync_state()
        by_id = {t.get("id"): t for t in existing_tracks if t.get("id") is not None}

        # Сначала оставляем из текущего буфера только те треки, которых ещё нет в YTMusic
        new_like_ids = []
        new_like_titles = []
        seen_ids = set()

        for tid in current.get("likeIds", []):
            if tid in disliked_ids or tid in seen_ids:
                continue
            t_obj = by_id.get(tid)
            if t_obj:
                vid = t_obj.get("videoId", "")
                if vid and vid in ytm_liked_vids:
                    continue  # Уже лайкнут в YouTube Music -> убираем из буфера
                seen_ids.add(tid)
                new_like_ids.append(tid)
                new_like_titles.append({
                    "id": tid,
                    "videoId": vid,
                    "title": t_obj.get("title", ""),
                    "artist": t_obj.get("artist", ""),
                })

        # Добавляем в буфер все треки с сайта с liked=True, которых нет в плейлисте лайков YTMusic
        added_missing = 0
        for t_obj in existing_tracks:
            tid = t_obj.get("id")
            vid = t_obj.get("videoId", "")
            if tid is None or not vid or tid in disliked_ids or tid in seen_ids:
                continue
            is_site_liked = bool(t_obj.get("liked")) or (t_obj.get("source") == "playlist")
            if is_site_liked and vid not in ytm_liked_vids:
                seen_ids.add(tid)
                new_like_ids.append(tid)
                new_like_titles.append({
                    "id": tid,
                    "videoId": vid,
                    "title": t_obj.get("title", ""),
                    "artist": t_obj.get("artist", ""),
                })
                added_missing += 1

        rem_listened = [] if reset_wave else [
            x for x in current.get("waveListenedIds", []) if x not in processed_listened_ids
        ]

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "updatedAt": now_iso,
            "bufferSyncedAt": now_iso,
            "likeIds": new_like_ids,
            "dislikeIds": current.get("dislikeIds", []),
            "siteLikedIds": current.get("siteLikedIds", []),
            "likedAtMap": current.get("likedAtMap", {}),
            "waveListenedIds": rem_listened,
            "likeTitles": new_like_titles,
            "dislikeTitles": current.get("dislikeTitles", []),
        }
        body = json.dumps({"files": {"nikrid_sync.json": {"content": json.dumps(payload, ensure_ascii=False, indent=2)}}}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            data=body,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "Nikrid-Radio-Sync",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                ui_line(
                    "✓",
                    f"Буфер лайков сверен с YTMusic: в буфере {len(new_like_ids)} (добавлено новых без лайка в YTM: {added_missing})",
                    _ANSI_GREEN,
                    step="gist_sync",
                )
    except Exception as e:
        ui_line("!", f"Не удалось сверить буфер в Gist ({_compact_err(e, 90)})", _ANSI_YELLOW, step="gist_sync", log_level="WARN")


# =========================================================
# БЫСТРОЕ И НАДЁЖНОЕ СКАЧИВАНИЕ АУДИО (YT-DLP)
# =========================================================
def load_unavailable_ids() -> set[str]:
    if not UNAVAILABLE_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(UNAVAILABLE_IDS_PATH.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_unavailable_ids(ids: set[str]):
    try:
        UNAVAILABLE_IDS_PATH.write_text(
            json.dumps(sorted(ids), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        _write_file_log("WARN", f"Не удалось сохранить {UNAVAILABLE_IDS_PATH}: {_compact_err(e)}", step="yt_dlp")


def download_audio_robust(video_id: str, unavailable_ids: set[str] | None = None, track_label: str = "") -> tuple[Path | None, bool]:
    """
    Скачивает аудио во временную папку.
    Возвращает (path_or_none, is_permanent_unavailable).
    """
    if unavailable_ids is not None and video_id in unavailable_ids:
        _write_file_log("INFO", f"[{video_id}] ({track_label}) пропущен по {UNAVAILABLE_IDS_PATH}", step="yt_dlp")
        return None, False

    TEMP_AUDIO_DIR.mkdir(exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    has_cookies = Path("cookies.txt").exists()

    # В GitHub Actions (или если есть cookies.txt) пробуем сначала нативный профиль, затем cookies
    profiles = [
        {"name": "native_default", "clients": None, "use_cookies": False, "fmt": "251/140/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"},
        {"name": "visionos_vr", "clients": ["visionos", "android_vr"], "use_cookies": False, "fmt": "bestaudio/best"},
        {"name": "cookies_fallback", "clients": None, "use_cookies": True, "fmt": "bestaudio/best"},
    ]

    if os.getenv("GITHUB_ACTIONS") == "true" and has_cookies:
        # На серверах GitHub сразу ставим cookies первым + гибкий выбор формата и EJS-решатель (Deno)
        profiles = [
            {"name": "cookies_ci", "clients": None, "use_cookies": True, "fmt": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"},
            {"name": "cookies_tv", "clients": ["tv", "web_safari"], "use_cookies": True, "fmt": "bestaudio/best"},
            {"name": "android_vr", "clients": ["android_vr", "visionos"], "use_cookies": False, "fmt": "bestaudio/best"},
        ]

    permanent_unavailable = False
    last_err = ""

    for attempt, prof in enumerate(profiles, start=1):
        prof_name = prof["name"]
        if prof["use_cookies"] and not has_cookies:
            continue

        out_tmpl = str(TEMP_AUDIO_DIR / f"{video_id}.%(ext)s")
        ydl_opts = {
            "format": prof["fmt"],
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "retries": 1,
            "fragment_retries": 1,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 12,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "remote_components": ["ejs:github"],
        }

        if prof["clients"]:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": prof["clients"]}}

        if prof["use_cookies"]:
            ydl_opts["cookiefile"] = "cookies.txt"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            for f in TEMP_AUDIO_DIR.glob(f"{video_id}.*"):
                if f.is_file() and f.stat().st_size > 10_000 and not f.name.endswith(".part"):
                    return f, False
            last_err = "Файл не найден или пуст после yt-dlp"
        except Exception as e:
            err_clean = _compact_err(e, 180)
            last_err = f"[{prof_name}] {err_clean}"
            _write_file_log("WARN", f"[{video_id}] ({track_label}) попытка {attempt} {last_err}", step="yt_dlp")
            lower_err = err_clean.lower()
            if (
                "video unavailable" in lower_err
                or "this video is not available" in lower_err
                or "private video" in lower_err
                or "removed by the uploader" in lower_err
                or "account associated with this video has been terminated" in lower_err
                or "copyright" in lower_err
                or "members-only" in lower_err
                or "join this channel" in lower_err
            ):
                permanent_unavailable = True
                break
            if "age-restricted" in lower_err or "sign in to confirm your age" in lower_err:
                if prof["use_cookies"] or not has_cookies:
                    permanent_unavailable = True
            time.sleep(0.3)

    if permanent_unavailable:
        ui_line("×", f"Пропущен (недоступен на YouTube): {track_label} [{video_id}]", _ANSI_DIM, step="yt_dlp", log_level="ERROR")
    else:
        ui_line("!", f"Ошибка скачивания: {track_label} [{video_id}] — {last_err}", _ANSI_RED, step="yt_dlp", log_level="ERROR")

    return None, permanent_unavailable


def parse_duration_sec(dur_str: str) -> int:
    if not dur_str or ":" not in str(dur_str):
        return 210
    parts = [int(p) for p in str(dur_str).split(":") if p.isdigit()]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 210


def extract_track_meta(raw_item: dict, source: str) -> dict | None:
    vid = raw_item.get("videoId")
    if not vid:
        return None
    title = raw_item.get("title") or "Без названия"
    artists = raw_item.get("artists") or []
    artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name")) or "Неизвестный исполнитель"
    thumbs = raw_item.get("thumbnails") or []
    cover = thumbs[-1].get("url", "") if thumbs else ""
    dur = raw_item.get("duration") or raw_item.get("length") or "3:30"

    return {
        "videoId": vid,
        "title": title,
        "artist": artist_name,
        "duration": dur,
        "seconds": parse_duration_sec(dur),
        "moods": ["Весёлая"],
        "source": source,
        "liked": source == "playlist",
        "image": cover
    }


def fetch_wave_200(yt: YTMusic, liked_seeds: list[str], exclude_vids: set[str]) -> list[dict]:
    wave_items = []
    seen = set(exclude_vids)

    for seed_vid in liked_seeds[:20]:
        if len(wave_items) >= WAVE_TARGET_COUNT:
            break
        try:
            watch = yt.get_watch_playlist(videoId=seed_vid, limit=30)
            for item in watch.get("tracks", []):
                meta = extract_track_meta(item, source="wave")
                if meta and meta["videoId"] not in seen:
                    seen.add(meta["videoId"])
                    wave_items.append(meta)
                    if len(wave_items) >= WAVE_TARGET_COUNT:
                        break
        except Exception as e:
            ui_line("!", f"Волна: сбой подбора от seed {seed_vid} ({_compact_err(e, 90)})", _ANSI_YELLOW, step="wave_fetch", log_level="WARN")

    return wave_items[:WAVE_TARGET_COUNT]


def save_playlist_json(tracks: list[dict]):
    tmp_path = PLAYLIST_JSON_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(tracks, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp_path.replace(PLAYLIST_JSON_PATH)


def _format_track_name(track: dict, max_len: int = 40) -> str:
    artist = (track.get("artist") or "").strip()
    title = (track.get("title") or "Без названия").strip()
    label = f"{artist} — {title}" if artist else title
    return label if len(label) <= max_len else label[: max_len - 3] + "..."


def main():
    parser = argparse.ArgumentParser(description="Nikrid Radio Sync (Яндекс Диск + обложки 512x512 WebP)")
    parser.add_argument("--limit", type=int, default=0, help="Ограничить число скачиваемых/мигрируемых аудио за запуск (например --limit 50)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Количество параллельных потоков (по умолчанию {MAX_WORKERS})")
    parser.add_argument("--covers-only", action="store_true", help="Скачать/сжать только локальные обложки 512x512 в covers/")
    parser.add_argument("--skip-yt-sync", action="store_true", help="Не запрашивать новые треки из YouTube Music")
    parser.add_argument("--reset-wave", action="store_true", help="Полностью сбросить старую волну и набрать свежие треки")
    args = parser.parse_args()

    workers = max(1, min(10, args.workers))
    start_ts = time.time()
    io_lock = threading.Lock()
    stats = {
        "migrated_ok": 0,
        "migrated_linked": 0,
        "new_ok": 0,
        "errors": 0,
        "covers_ok": 0,
    }

    ui_section("Nikrid Radio Sync")
    _write_file_log("INFO", f"=== Старт синхронизации (args={vars(args)}) ===", step="init")

    try:
        yadisk_token = load_yadisk_token()
        ensure_yadisk_folder(yadisk_token)
        existing_yadisk_files = fetch_existing_yadisk_files(yadisk_token)
        ui_line("✓", f"Яндекс Диск готов ({YADISK_ROOT_DIR}, файлов: {len(existing_yadisk_files)})", _ANSI_GREEN, step="init")
    except Exception as e:
        ui_line("✕", f"Критическая ошибка Яндекс Диска: {_compact_err(e, 160)}", _ANSI_RED, step="yadisk_auth", log_level="ERROR")
        sys.exit(1)

    # 1. Загружаем текущий playlist.json
    existing_tracks = []
    if PLAYLIST_JSON_PATH.exists():
        try:
            existing_tracks = json.loads(PLAYLIST_JSON_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            ui_line("!", f"Ошибка чтения {PLAYLIST_JSON_PATH} ({_compact_err(e, 100)}) — начат пустой список", _ANSI_YELLOW, step="playlist_load", log_level="WARN")
            existing_tracks = []

    try:
        # 2. Подтягиваем облачный буфер лайков/дизлайков/прослушиваний
        cloud = load_cloud_sync_state()
        disliked_ids = set(cloud.get("dislikeIds", []))
        liked_ids = set(cloud.get("likeIds", []))
        wave_listened_ids = set(cloud.get("waveListenedIds", []))

        disliked_vids = set()
        cleaned_tracks = []
        removed_count = 0
        for t in existing_tracks:
            if "source" not in t:
                t["source"] = "playlist"
            if "liked" not in t:
                t["liked"] = t["source"] == "playlist"

            tid = t.get("id")
            vid = t.get("videoId", "")
            is_liked = (tid in liked_ids) or bool(t.get("liked")) or (t.get("source") == "playlist")
            is_listened_wave = (t.get("source") == "wave") and (tid in wave_listened_ids)

            if tid in disliked_ids or (is_listened_wave and not is_liked):
                removed_count += 1
                if vid:
                    disliked_vids.add(vid)
                    delete_local_cover(vid)
                if t.get("yadiskPath"):
                    delete_from_yadisk(yadisk_token, t["yadiskPath"])
                reason = "дизлайк" if tid in disliked_ids else "прослушан в волне"
                _write_file_log("INFO", f"Удалён трек ({reason}): {t.get('title')} [{vid}]", step="cleanup")
                continue

            if tid in liked_ids:
                t["liked"] = True
                t["source"] = "playlist"
            cleaned_tracks.append(t)

        existing_tracks = cleaned_tracks
        if removed_count > 0:
            ui_line("•", f"Очищено по буферу/волне: {removed_count} трек(ов)", _ANSI_DIM, step="cleanup")

        unavailable_ids = load_unavailable_ids()
        before_bl = len(existing_tracks)
        kept_tracks = []
        for t in existing_tracks:
            vid_t = t.get("videoId", "")
            if vid_t and vid_t in unavailable_ids and not t.get("yadiskPublicUrl"):
                delete_local_cover(vid_t)
                continue
            kept_tracks.append(t)
        if len(kept_tracks) < before_bl:
            ui_line("•", f"Удалено нерабочих треков по чёрному списку: {before_bl - len(kept_tracks)}", _ANSI_DIM, step="cleanup")
        existing_tracks = kept_tracks

        by_vid = {t.get("videoId"): t for t in existing_tracks if t.get("videoId")}
        next_numeric_id = max((t.get("id", 0) for t in existing_tracks), default=0) + 1

        # 3. Локальные обложки 512x512 WebP в covers/ (параллельно)
        ui_section("Обложки (512×512 WebP)")
        covers_missing = [
            t for t in existing_tracks
            if t.get("videoId") and not (t.get("image") == f"covers/{t['videoId']}.webp" and (COVERS_DIR / f"{t['videoId']}.webp").exists())
        ]

        if not covers_missing:
            ui_line("✓", f"Все обложки на месте ({len(existing_tracks)} треков)", _ANSI_GREEN, step="covers")
        else:
            completed_c = 0
            saved_c = 0

            def _cover_one(t_item):
                vid_c = t_item["videoId"]
                rel = ensure_local_cover(vid_c, t_item.get("image", ""))
                return t_item, rel

            with ThreadPoolExecutor(max_workers=COVER_WORKERS) as pool:
                futures = [pool.submit(_cover_one, t) for t in covers_missing]
                for fut in as_completed(futures):
                    completed_c += 1
                    t_item, rel = fut.result()
                    with io_lock:
                        if rel:
                            t_item["image"] = rel
                            stats["covers_ok"] += 1
                            saved_c += 1
                            if saved_c >= 25:
                                save_playlist_json(existing_tracks)
                                saved_c = 0
                        ui_progress("Обложки", completed_c, len(covers_missing), _format_track_name(t_item, 32))

            ui_clear_inline()
            existing_tracks = [
                t for t in existing_tracks
                if not (t.get("videoId") in unavailable_ids and not t.get("yadiskPublicUrl"))
            ]
            save_playlist_json(existing_tracks)
            ui_line("✓", f"Обложки готовы: скачано/сжато {stats['covers_ok']} шт.", _ANSI_GREEN, step="covers")

        if args.covers_only:
            elapsed = time.time() - start_ts
            ui_section("Итог")
            ui_line("✓", f"Режим --covers-only завершён за {elapsed:.1f} с", _ANSI_GREEN, step="summary")
            return

        # 4. Миграция существующих треков из playlist.json на Яндекс Диск (параллельно)
        need_migrate = [t for t in existing_tracks if t.get("videoId") and not t.get("yadiskPublicUrl")]
        audio_ops_done = 0

        if need_migrate:
            batch_migrate = need_migrate[: args.limit] if args.limit > 0 else need_migrate
            if args.limit > 0 and len(need_migrate) > args.limit:
                ui_line("•", f"Ограничено флагом --limit {args.limit} (из {len(need_migrate)})", _ANSI_CYAN, step="migrate")
            ui_section(f"Миграция аудио на Яндекс Диск ({len(batch_migrate)} треков, потоков: {workers})")

            completed_m = 0
            saved_batch = 0

            def _migrate_one(t_item):
                vid_m = t_item["videoId"]
                name_m = _format_track_name(t_item, 36)

                with io_lock:
                    existing_info = existing_yadisk_files.get(vid_m)
                    is_unavail = vid_m in unavailable_ids

                if existing_info:
                    try:
                        r_path = existing_info["path"]
                        p_url = existing_info["public_url"] or publish_yadisk_resource(yadisk_token, r_path)
                        return ("linked", t_item, vid_m, name_m, r_path, p_url, False, None)
                    except Exception as exc:
                        return ("fail_up", t_item, vid_m, name_m, "", "", False, exc)

                if is_unavail:
                    return ("skip_unavail", t_item, vid_m, name_m, "", "", False, None)

                loc_path, perm_unavail = download_audio_robust(vid_m, None, track_label=name_m)
                if not loc_path:
                    return ("fail_dl", t_item, vid_m, name_m, "", "", perm_unavail, None)

                try:
                    r_path, p_url = upload_and_publish_yadisk(yadisk_token, loc_path, vid_m)
                    return ("uploaded", t_item, vid_m, name_m, r_path, p_url, False, None)
                except Exception as exc:
                    return ("fail_up", t_item, vid_m, name_m, "", "", False, exc)
                finally:
                    try:
                        if loc_path.exists():
                            loc_path.unlink()
                    except Exception:
                        pass

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_migrate_one, item) for item in batch_migrate]
                for fut in as_completed(futures):
                    completed_m += 1
                    kind, t_item, vid_m, name_m, r_path, p_url, perm_unavail, exc = fut.result()
                    with io_lock:
                        if perm_unavail:
                            unavailable_ids.add(vid_m)
                            save_unavailable_ids(unavailable_ids)

                        if kind == "linked":
                            t_item["yadiskPath"] = r_path
                            t_item["yadiskPublicUrl"] = p_url
                            t_item.pop("driveId", None)
                            t_item.pop("audioUrl", None)
                            stats["migrated_linked"] += 1
                            saved_batch += 1
                            ui_progress("Миграция", completed_m, len(batch_migrate), f"связан: {name_m}")
                        elif kind == "uploaded":
                            t_item["yadiskPath"] = r_path
                            t_item["yadiskPublicUrl"] = p_url
                            t_item.pop("driveId", None)
                            t_item.pop("audioUrl", None)
                            existing_yadisk_files[vid_m] = {"path": r_path, "public_url": p_url}
                            audio_ops_done += 1
                            stats["migrated_ok"] += 1
                            saved_batch += 1
                            ui_progress("Миграция", completed_m, len(batch_migrate), f"✓ {name_m}")
                        elif kind == "fail_up":
                            stats["errors"] += 1
                            ui_line("!", f"Ошибка Яндекс Диска {name_m} [{vid_m}]: {_compact_err(exc, 110)}", _ANSI_RED, step="migrate", log_level="ERROR")
                        else:
                            stats["errors"] += 1
                            ui_progress("Миграция", completed_m, len(batch_migrate), f"пропуск: {name_m}")

                        if saved_batch >= 5:
                            save_playlist_json(existing_tracks)
                            saved_batch = 0

            ui_clear_inline()
            save_playlist_json(existing_tracks)

        # 5. Синхронизация новых треков из YouTube Music (параллельно)
        if not args.skip_yt_sync and (args.limit == 0 or audio_ops_done < args.limit):
            ui_section("Синхронизация с YouTube Music")
            try:
                yt = get_ytmusic_client()
                ui_status("↻", "Запрашиваю лайкнутые треки...", _ANSI_CYAN, step="yt_sync")
                yt_liked_raw = yt.get_liked_songs(limit=1000).get("tracks", [])
                liked_metas = []
                liked_vids = []
                for idx_l, item in enumerate(yt_liked_raw):
                    meta = extract_track_meta(item, source="playlist")
                    if meta and meta["videoId"] not in disliked_vids:
                        meta["playlistOrder"] = idx_l
                        meta["inYtmLiked"] = True
                        liked_metas.append(meta)
                        liked_vids.append(meta["videoId"])

                ytm_liked_vids_set = set(liked_vids)
                for t_ex in existing_tracks:
                    vid_ex = t_ex.get("videoId", "")
                    if vid_ex in ytm_liked_vids_set:
                        t_ex["inYtmLiked"] = True
                    else:
                        t_ex["inYtmLiked"] = False

                dropped_wave_vids = set()
                if args.reset_wave:
                    kept_non_wave = []
                    for t_w in existing_tracks:
                        if t_w.get("source") == "wave" and not t_w.get("liked") and t_w.get("id") not in liked_ids:
                            vid_w = t_w.get("videoId", "")
                            if vid_w:
                                dropped_wave_vids.add(vid_w)
                                delete_local_cover(vid_w)
                            if t_w.get("yadiskPath"):
                                delete_from_yadisk(yadisk_token, t_w["yadiskPath"])
                            continue
                        kept_non_wave.append(t_w)
                    existing_tracks = kept_non_wave
                    by_vid = {t.get("videoId"): t for t in existing_tracks if t.get("videoId")}
                    ui_line("✓", f"Волна сброшена: удалено старых wave-треков {len(dropped_wave_vids)}", _ANSI_GREEN, step="yt_sync")

                current_wave_total = [t for t in existing_tracks if t.get("source") == "wave"]
                need_wave = max(0, WAVE_TARGET_COUNT - len(current_wave_total))
                wave_metas = []
                if need_wave > 0:
                    ui_status("↻", f"Подбираю «Мою волну» ({need_wave} треков)...", _ANSI_CYAN, step="yt_sync")
                    exclude_set = ytm_liked_vids_set | disliked_vids | set(by_vid.keys()) | dropped_wave_vids
                    seed_pool = list(liked_vids)
                    random.shuffle(seed_pool)
                    wave_metas = fetch_wave_200(yt, seed_pool, exclude_set)[:need_wave]

                ui_clear_inline()
                ui_line("✓", f"YTMusic: лайков {len(liked_metas)}, новых в волну {len(wave_metas)}", _ANSI_GREEN, step="yt_sync")

                to_process = []
                for meta in (liked_metas + wave_metas):
                    vid_m = meta["videoId"]
                    if vid_m in by_vid:
                        if meta["source"] == "playlist":
                            by_vid[vid_m]["source"] = "playlist"
                            by_vid[vid_m]["liked"] = True
                            by_vid[vid_m]["inYtmLiked"] = True
                            by_vid[vid_m]["playlistOrder"] = meta.get("playlistOrder")
                        by_vid[vid_m]["moods"] = ["Весёлая"]
                    else:
                        to_process.append(meta)

                remaining_quota = (args.limit - audio_ops_done) if args.limit > 0 else len(to_process)
                if remaining_quota > 0 and to_process:
                    batch_new = to_process[:remaining_quota]
                    saved_batch = 0
                    completed_n = 0

                    def _fetch_new_one(m_item):
                        vid_n = m_item["videoId"]
                        name_n = _format_track_name(m_item, 36)
                        with io_lock:
                            is_unavail = vid_n in unavailable_ids

                        if is_unavail:
                            return ("skip_unavail", m_item, vid_n, name_n, "", "", "", False, None)

                        loc_p, perm_unavail = download_audio_robust(vid_n, None, track_label=name_n)
                        if not loc_p:
                            return ("fail_dl", m_item, vid_n, name_n, "", "", "", perm_unavail, None)

                        try:
                            r_p, p_u = upload_and_publish_yadisk(yadisk_token, loc_p, vid_n)
                            cov_p = ensure_local_cover(vid_n, m_item.get("image", ""))
                            return ("uploaded", m_item, vid_n, name_n, r_p, p_u, cov_p, False, None)
                        except Exception as exc:
                            return ("fail_up", m_item, vid_n, name_n, "", "", "", False, exc)
                        finally:
                            try:
                                if loc_p.exists():
                                    loc_p.unlink()
                            except Exception:
                                pass

                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(_fetch_new_one, m) for m in batch_new]
                        for fut in as_completed(futures):
                            completed_n += 1
                            kind, m_item, vid_n, name_n, r_p, p_u, cov_p, perm_unavail, exc = fut.result()
                            with io_lock:
                                if perm_unavail:
                                    unavailable_ids.add(vid_n)
                                    save_unavailable_ids(unavailable_ids)

                                if kind == "uploaded":
                                    new_entry = {
                                        "id": next_numeric_id,
                                        "videoId": vid_n,
                                        "yadiskPath": r_p,
                                        "yadiskPublicUrl": p_u,
                                        "title": m_item["title"],
                                        "artist": m_item["artist"],
                                        "duration": m_item["duration"],
                                        "seconds": m_item["seconds"],
                                        "moods": ["Весёлая"],
                                        "source": m_item["source"],
                                        "liked": m_item["liked"],
                                        "inYtmLiked": bool(m_item.get("inYtmLiked", False)),
                                        "playlistOrder": m_item.get("playlistOrder"),
                                        "image": cov_p
                                    }
                                    next_numeric_id += 1
                                    existing_tracks.append(new_entry)
                                    by_vid[vid_n] = new_entry
                                    audio_ops_done += 1
                                    stats["new_ok"] += 1
                                    if cov_p:
                                        stats["covers_ok"] += 1
                                    saved_batch += 1
                                    ui_progress("YTMusic", completed_n, len(batch_new), f"✓ {name_n}")
                                elif kind == "fail_up":
                                    stats["errors"] += 1
                                    ui_line("!", f"Ошибка загрузки {name_n} [{vid_n}]: {_compact_err(exc, 110)}", _ANSI_RED, step="yt_sync", log_level="ERROR")
                                else:
                                    stats["errors"] += 1
                                    ui_progress("YTMusic", completed_n, len(batch_new), f"пропуск: {name_n}")

                                if saved_batch >= 5:
                                    save_playlist_json(existing_tracks)
                                    saved_batch = 0

                    ui_clear_inline()
            except Exception as e:
                ui_line("!", f"Сбой авторизации/запроса YouTube Music: {_compact_err(e, 140)}", _ANSI_RED, step="yt_sync", log_level="ERROR")

        if not args.skip_yt_sync and "ytm_liked_vids_set" in locals():
            reconcile_like_buffer_in_gist(
                existing_tracks,
                ytm_liked_vids_set,
                disliked_ids,
                wave_listened_ids,
                reset_wave=args.reset_wave,
            )
        else:
            clear_cloud_sync_buffer(set(), set(), wave_listened_ids)

    except KeyboardInterrupt:
        ui_line("!", "Остановлено пользователем (Ctrl+C) — сохраняю прогресс...", _ANSI_YELLOW, step="interrupt", log_level="INFO")
    finally:
        if existing_tracks:
            save_playlist_json(existing_tracks)
        shutil.rmtree(TEMP_AUDIO_DIR, ignore_errors=True)

    elapsed = time.time() - start_ts
    migrated_count = sum(1 for t in existing_tracks if t.get("yadiskPublicUrl"))

    ui_section("Итог")
    ui_line("✓", f"Время: {elapsed:.1f} с | Всего в playlist.json: {len(existing_tracks)} (на Яндекс Диске: {migrated_count})", _ANSI_GREEN, step="summary")
    ui_line("•", f"Загружено новых: {stats['new_ok']}, мигрировано: {stats['migrated_ok']}, связано готовых: {stats['migrated_linked']}, обложек: {stats['covers_ok']}", _ANSI_DIM, step="summary")
    if stats["errors"] > 0:
        ui_line("!", f"Ошибок/пропусков: {stats['errors']} — подробности в {FAILED_LOG_PATH}", _ANSI_YELLOW, step="summary", log_level="INFO")
    _write_file_log("INFO", f"=== Завершено за {elapsed:.1f} с (total={len(existing_tracks)}, on_disk={migrated_count}, errors={stats['errors']}) ===", step="summary")


if __name__ == "__main__":
    main()