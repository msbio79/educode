"""Notion 데이터베이스 → items.json + thumbs/ 동기화 스크립트.

환경변수:
  NOTION_TOKEN        Notion 통합(Integration) 시크릿
  NOTION_DATABASE_ID  시뮬레이션 목록 데이터베이스 ID

각 항목에서 다음을 자동으로 찾습니다.
  - 제목(title) 속성      → 이름
  - URL 속성(없으면 텍스트에서 http로 시작하는 값) → 주소
  - 다중 선택/선택 속성   → 카테고리
  - 썸네일: 페이지 본문의 첫 번째 이미지 → 없으면 페이지 커버 → 없으면 파일 속성의 첫 이미지

Notion 이미지 주소는 1시간 뒤 만료되므로 thumbs/ 폴더에 내려받아 저장합니다.
이미지가 바뀌지 않았으면 다시 받지 않습니다.
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.notion.com/v1"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "items.json"
THUMBS = ROOT / "thumbs"
THUMB_WIDTH = 640
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


class NotionError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def notion(path, body=None):
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        fail("NOTION_TOKEN 시크릿이 비어 있습니다. GitHub 저장소 Settings → Secrets and variables → Actions 에 등록하세요.")
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            info = json.load(e)
        except Exception:
            info = {}
        raise NotionError(e.code, info.get("code", ""), info.get("message", e.reason)) from None


def fail(msg):
    # ::error:: 형식은 GitHub Actions 화면의 Annotations 에 그대로 표시됩니다.
    print(f"::error::{msg}")
    sys.exit(1)


def find_data_sources(db_id):
    """데이터베이스 ID → 데이터 소스 ID 목록. 못 찾으면 연결된 데이터 소스를 검색합니다."""
    try:
        db = notion(f"/databases/{db_id}")
        sources = [s["id"] for s in db.get("data_sources", [])]
        if sources:
            return sources
    except NotionError as e:
        print(f"데이터베이스 {db_id} 를 직접 열지 못했습니다 ({e}). 연결된 데이터베이스를 검색합니다.")

    found = notion("/search", {"filter": {"property": "object", "value": "data_source"}, "page_size": 100})
    sources = found.get("results", [])
    for s in sources:
        print(f"  - 찾은 데이터베이스: {plain(s.get('title'))} ({s['id']})")
    if len(sources) == 1:
        return [sources[0]["id"]]
    if not sources:
        fail("Notion 연결이 접근할 수 있는 데이터베이스가 없습니다. Notion 개발자 연결 → 콘텐츠 사용 권한에 '과학 시뮬레이션 도구' 페이지를 추가했는지 확인하세요.")
    # 여러 개면 URL 속성이 있는 것 중 첫 번째
    for s in sources:
        if any(p.get("type") == "url" for p in s.get("properties", {}).values()):
            print(f"URL 속성이 있는 '{plain(s.get('title'))}' 를 사용합니다. 다르면 NOTION_DATABASE_ID 변수를 설정하세요.")
            return [s["id"]]
    fail("데이터베이스가 여러 개라 고를 수 없습니다. 위 목록에서 맞는 ID를 NOTION_DATABASE_ID 변수로 등록하세요.")


def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich or []).strip()


def file_url(f):
    """Notion file 객체(file/external)에서 주소를 꺼냅니다."""
    if not f:
        return None
    return (f.get("file") or f.get("external") or {}).get("url")


def find_thumb(page):
    """(고유 ID, 버전, 이미지 주소)를 돌려줍니다. 없으면 None."""
    try:
        blocks = notion(f"/blocks/{page['id']}/children?page_size=50")["results"]
    except Exception as e:  # 본문을 못 읽어도 나머지는 계속 진행
        print(f"  ! 본문 읽기 실패 ({page['id']}): {e}")
        blocks = []
    for b in blocks:
        if b["type"] == "image":
            url = file_url(b["image"])
            if url:
                return b["id"], b["last_edited_time"], url
    if page.get("cover"):
        url = file_url(page["cover"])
        if url:
            return page["id"] + "-cover", page["last_edited_time"], url
    for prop in page["properties"].values():
        if prop["type"] == "files":
            for f in prop["files"]:
                url = file_url(f)
                if url and url.split("?")[0].lower().endswith(IMAGE_EXT):
                    return page["id"] + "-files", page["last_edited_time"], url
    return None


def save_thumb(key, url):
    """이미지를 내려받아 작게 줄여 저장하고, 사이트 기준 경로를 돌려줍니다."""
    req = urllib.request.Request(url, headers={"User-Agent": "educode-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    name = key.replace("-", "")
    try:
        from PIL import Image  # GitHub Actions에서 설치됨

        img = Image.open(io.BytesIO(data))
        img.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        path = THUMBS / f"{name}.webp"
        img.save(path, "WEBP", quality=82)
    except ImportError:
        ext = Path(url.split("?")[0]).suffix.lower()
        path = THUMBS / f"{name}{ext if ext in IMAGE_EXT else '.png'}"
        path.write_bytes(data)
    return f"thumbs/{path.name}"


def to_item(page):
    title, url, tags = "", "", []
    for prop in page["properties"].values():
        kind = prop["type"]
        if kind == "title":
            title = plain(prop["title"])
        elif kind == "url" and prop["url"] and not url:
            url = prop["url"].strip()
        elif kind == "rich_text" and not url:
            text = plain(prop["rich_text"])
            if text.startswith("http"):
                url = text
        elif kind == "multi_select":
            tags += [o["name"] for o in prop["multi_select"]]
        elif kind == "select" and prop["select"]:
            tags.append(prop["select"]["name"])
    if url and not url.startswith("http"):
        url = "https://" + url
    if not title or not url.startswith("http"):
        return None
    return {"title": title, "url": url, "tags": tags or ["기타"]}


def main():
    db = os.environ.get("NOTION_DATABASE_ID", "").replace("-", "").strip()
    pages = []
    for ds in find_data_sources(db):
        cursor = None
        while True:
            body = {
                "page_size": 100,
                "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            }
            if cursor:
                body["start_cursor"] = cursor
            res = notion(f"/data_sources/{ds}/query", body)
            pages += [p for p in res["results"] if p.get("object") == "page"]
            if not res.get("has_more"):
                break
            cursor = res["next_cursor"]
    pages.sort(key=lambda p: p["created_time"], reverse=True)
    print(f"Notion에서 {len(pages)}개 페이지를 읽었습니다.")

    # 이전 결과: 썸네일 재사용용
    old = {}
    if OUT.exists():
        for i in json.loads(OUT.read_text(encoding="utf-8")):
            if i.get("thumbKey"):
                old[i["thumbKey"]] = i
    THUMBS.mkdir(exist_ok=True)

    items = []
    for page in pages:
        item = to_item(page)
        if not item:
            continue
        found = find_thumb(page)
        if found:
            key, version, img_url = found
            prev = old.get(key)
            if prev and prev.get("thumbVersion") == version and (ROOT / prev["thumb"]).exists():
                item["thumb"] = prev["thumb"]
            else:
                try:
                    item["thumb"] = save_thumb(key, img_url)
                    print(f"  썸네일 저장: {item['title']}")
                except Exception as e:
                    print(f"  ! 썸네일 실패 ({item['title']}): {e}")
            if item.get("thumb"):
                item["thumbKey"], item["thumbVersion"] = key, version
        items.append(item)

    if not items:
        fail("Notion에서 항목을 하나도 가져오지 못했습니다(제목·URL 속성 확인). items.json 을 그대로 둡니다.")

    # 더 이상 쓰지 않는 썸네일 정리
    used = {i["thumb"] for i in items if i.get("thumb")}
    for f in THUMBS.iterdir():
        if f"thumbs/{f.name}" not in used:
            f.unlink()

    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)}개 항목을 items.json 에 저장했습니다.")


if __name__ == "__main__":
    try:
        main()
    except NotionError as e:
        hint = {
            401: "토큰이 올바르지 않습니다. NOTION_TOKEN 시크릿을 다시 확인하세요.",
            403: "연결에 '콘텐츠 읽기' 권한이 없습니다.",
            404: "연결이 이 데이터베이스에 접근할 수 없습니다. 콘텐츠 사용 권한을 확인하세요.",
        }.get(e.status, "")
        fail(f"Notion API 오류 {e} {hint}")
