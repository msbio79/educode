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
import urllib.request
from pathlib import Path

API = "https://api.notion.com/v1"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "items.json"
THUMBS = ROOT / "thumbs"
THUMB_WIDTH = 640
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def notion(path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


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
    db = os.environ["NOTION_DATABASE_ID"].replace("-", "")
    pages, cursor = [], None
    while True:
        body = {
            "page_size": 100,
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        if cursor:
            body["start_cursor"] = cursor
        res = notion(f"/databases/{db}/query", body)
        pages += res["results"]
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]

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
        sys.exit("Notion에서 항목을 하나도 가져오지 못했습니다. items.json 을 그대로 둡니다.")

    # 더 이상 쓰지 않는 썸네일 정리
    used = {i["thumb"] for i in items if i.get("thumb")}
    for f in THUMBS.iterdir():
        if f"thumbs/{f.name}" not in used:
            f.unlink()

    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)}개 항목을 items.json 에 저장했습니다.")


if __name__ == "__main__":
    main()
