#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""관리자용: 오보/오류 기사를 게시된 사이트에서 즉시 제거하고 영구 차단 목록에 등록.

사용법: python admin_remove_article.py "삭제할 기사 헤드라인 일부"
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


def main():
    if len(sys.argv) < 2:
        print('사용법: python admin_remove_article.py "삭제할 기사 헤드라인 일부"')
        sys.exit(1)
    query = sys.argv[1]

    tmp = tempfile.mkdtemp(prefix="ghp_admin_")
    try:
        print(f"gh-pages 클론 중... ({tmp})")
        run(["git", "clone", "--depth", "1", "--branch", "gh-pages", REPO_URL, tmp], cwd=os.getcwd())

        idx_path = os.path.join(tmp, "index.html")
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
            print(f"'{query}'를 포함하는 기사를 찾지 못했습니다. 헤드라인 일부를 정확히 붙여넣어 주세요.")
            sys.exit(1)

        i, item = matched
        m = re.search(r'data-src-title="([^"]*)"', item)
        src_title = m.group(1).replace("&quot;", '"') if m else None
        headline_m = re.search(r'<div class="news-headline">(.*?)</div>', item, re.S)
        headline = headline_m.group(1).strip() if headline_m else "(제목 추출 실패)"

        print(f"찾은 기사: {headline}")
        print(f"원문 제목(영구 차단 등록용): {src_title!r}")

        # 해당 블록 제거
        blocks[i] = blocks[i][len(item):]
        new_html = "".join(blocks)
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(new_html)

        # blacklist.json에 원문 제목 영구 등록 (다음 실행부터도 재수집 차단)
        bl_path = os.path.join(tmp, "blacklist.json")
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
