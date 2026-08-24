#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""관리자용: 오보/오류 기사를 게시된 사이트에서 즉시 제거하고 영구 차단 목록에 등록.

로컬 사용법: python admin_remove_article.py "삭제할 기사 헤드라인 일부" [--dry-run]
CI 사용법 (이미 체크아웃된 gh-pages 디렉토리에 적용, git 커밋/푸시는 호출자가 처리):
    python admin_remove_article.py "..." --dir public
"""
import sys, os, re, json, shutil, subprocess, tempfile

REPO_URL = "https://github.com/juno99zz-arkt/Hr-Daily-Brief.git"


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise RuntimeError(f"명령 실패: {' '.join(cmd)}")
    return r.stdout


def remove_article(repo_dir, query, dry_run=False):
    """repo_dir(gh-pages 체크아웃)에서 query를 포함하는 기사를 제거하고 blacklist.json에 등록.
    반환: (found: bool, headline: str, src_title: str)"""
    idx_path = os.path.join(repo_dir, "index.html")
    html = open(idx_path, encoding="utf-8").read()

    # news-item 블록 단위로 분리해 검색어를 포함하는 항목 탐색
    blocks = re.split(r'(?=<a class="news-item")', html)
    matched = None
    for i, b in enumerate(blocks):
        if not b.startswith('<a class="news-item"'):
            continue
        end = b.find("</a>")
        if end == -1:
            continue
        item = b[: end + 4]
        if query in item:
            matched = (i, item)
            break

    if not matched:
        return False, None, None

    i, item = matched
    m = re.search(r'data-src-title="([^"]*)"', item)
    src_title = m.group(1).replace("&quot;", '"') if m else None
    headline_m = re.search(r'<div class="news-headline">(.*?)</div>', item, re.S)
    headline = headline_m.group(1).strip() if headline_m else "(제목 추출 실패)"

    if dry_run:
        return True, headline, src_title

    # 해당 블록 제거
    blocks[i] = blocks[i][len(item):]
    new_html = "".join(blocks)
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    # blacklist.json에 원문 제목 영구 등록 (다음 실행부터도 재수집 차단)
    bl_path = os.path.join(repo_dir, "blacklist.json")
    titles = []
    if os.path.exists(bl_path):
        try:
            titles = json.loads(open(bl_path, encoding="utf-8").read()).get("titles", [])
        except Exception:
            titles = []
    if src_title and src_title not in titles:
        titles.append(src_title)
    with open(bl_path, "w", encoding="utf-8") as f:
        json.dump({"titles": titles}, f, ensure_ascii=False, indent=2)

    return True, headline, src_title


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv[1:]
    dir_idx = sys.argv.index("--dir") if "--dir" in sys.argv else None
    target_dir = sys.argv[dir_idx + 1] if dir_idx is not None else None

    if not args:
        print('사용법: python admin_remove_article.py "삭제할 기사 헤드라인 일부" [--dry-run] [--dir <경로>]')
        sys.exit(1)
    query = args[0]

    if target_dir:
        # CI 모드: 이미 체크아웃된 디렉토리에 적용, git 커밋/푸시는 워크플로가 처리
        found, headline, src_title = remove_article(target_dir, query, dry_run)
        if not found:
            print(f"'{query}'를 포함하는 기사를 찾지 못했습니다. 헤드라인 일부를 정확히 입력해 주세요.")
            sys.exit(1)
        print(f"찾은 기사: {headline}")
        print(f"원문 제목(영구 차단 등록용): {src_title!r}")
        if dry_run:
            print("\n[--dry-run] 실제 삭제는 수행하지 않았습니다.")
        else:
            print("\n완료: index.html에서 제거 + blacklist.json 등록됨. (커밋/푸시는 워크플로가 처리)")
        return

    # 로컬 모드: 직접 클론 → 수정 → 커밋 → 푸시
    tmp = tempfile.mkdtemp(prefix="ghp_admin_")
    try:
        print(f"gh-pages 클론 중... ({tmp})")
        run(["git", "clone", "--depth", "1", "--branch", "gh-pages", REPO_URL, tmp], cwd=os.getcwd())

        found, headline, src_title = remove_article(tmp, query, dry_run)
        if not found:
            print(f"'{query}'를 포함하는 기사를 찾지 못했습니다. 헤드라인 일부를 정확히 붙여넣어 주세요.")
            sys.exit(1)

        print(f"찾은 기사: {headline}")
        print(f"원문 제목(영구 차단 등록용): {src_title!r}")

        if dry_run:
            print("\n[--dry-run] 실제 삭제/커밋/푸시는 수행하지 않았습니다.")
            return

        run(["git", "add", "index.html", "blacklist.json"], cwd=tmp)
        run(
            ["git", "-c", "user.name=Admin", "-c", "user.email=admin@local",
             "commit", "-m", f"admin: 기사 삭제 - {headline[:40]}"],
            cwd=tmp,
        )
        run(["git", "push", "origin", "gh-pages"], cwd=tmp)

        print("\n완료: 사이트에서 즉시 제거 + 영구 차단 목록에 등록되어 향후 재수집도 차단됩니다.")
        print("배포 URL: https://juno99zz-arkt.github.io/Hr-Daily-Brief/")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
