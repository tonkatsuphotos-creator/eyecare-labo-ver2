#!/usr/bin/env python3
"""
Notionデータベースに記事情報を登録するスクリプト

Usage:
    python tools/notion_register.py --title "記事タイトル" --filepath "articles/xxx.md"

注意: NOTION_TOKEN は環境変数での管理を推奨します。
    export NOTION_TOKEN="your_token_here"
"""

import argparse
import os
import re
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

if not NOTION_TOKEN:
    print("[エラー] NOTION_TOKEN が未設定です。.env ファイルを確認してください。", file=sys.stderr)
    sys.exit(1)
if not DATABASE_ID:
    print("[エラー] NOTION_DATABASE_ID が未設定です。.env ファイルを確認してください。", file=sys.stderr)
    sys.exit(1)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_BLOCK_LIMIT = 100

GITHUB_OWNER = os.environ.get("GH_OWNER", "Rplus-shop")
GITHUB_REPO = os.environ.get("GH_REPO", "eyecare-labo")
GITHUB_ARTICLE_BASE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/main/articles"
GITHUB_IMAGE_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/images"


def to_github_article_url(filepath: str) -> str:
    return f"{GITHUB_ARTICLE_BASE}/{Path(filepath).name}"


def to_github_image_url(filepath: str) -> str:
    return f"{GITHUB_IMAGE_BASE}/{Path(filepath).stem}.png"


def build_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def parse_inline(text: str) -> list:
    """**bold** を含むテキストをNotionのrich_textリストに変換する"""
    parts = []
    last = 0
    for m in re.finditer(r"\*\*(.*?)\*\*", text):
        if m.start() > last:
            parts.append({"type": "text", "text": {"content": text[last:m.start()]}})
        parts.append({
            "type": "text",
            "text": {"content": m.group(1)},
            "annotations": {"bold": True},
        })
        last = m.end()
    if last < len(text):
        parts.append({"type": "text", "text": {"content": text[last:]}})
    return parts or [{"type": "text", "text": {"content": text}}]


def md_to_blocks(filepath: str) -> list:
    """MarkdownファイルをNotionブロックのリストに変換する"""
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()

    blocks = []
    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue

        # H1（## と区別するため先に ## を除外）
        if re.match(r"^# [^#]", stripped):
            blocks.append({
                "object": "block", "type": "heading_1",
                "heading_1": {"rich_text": parse_inline(stripped[2:])},
            })
            continue

        if stripped.startswith("## "):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": parse_inline(stripped[3:])},
            })
            continue

        if stripped.startswith("### "):
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": parse_inline(stripped[4:])},
            })
            continue

        m = re.match(r"^\d+\.\s+(.*)", stripped)
        if m:
            blocks.append({
                "object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": parse_inline(m.group(1))},
            })
            continue

        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": parse_inline(stripped)},
        })

    return blocks


def append_blocks(page_id: str, blocks: list) -> None:
    """ページにブロックを追記する（100件超の場合に使用）"""
    resp = requests.patch(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=build_headers(),
        json={"children": blocks},
        timeout=10,
    )
    if resp.status_code != 200:
        data = resp.json()
        print(f"[エラー] ブロック追記失敗 {resp.status_code}: {data.get('message')}", file=sys.stderr)
        sys.exit(1)


def build_payload(title: str, filepath: str, first_blocks: list) -> dict:
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "名前": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "ステータス": {
                "select": {"name": "生成済"}
            },
            "生成記事リンク": {
                "url": to_github_article_url(filepath)
            },
            "生成画像リンク": {
                "url": to_github_image_url(filepath)
            },
        },
    }
    if first_blocks:
        payload["children"] = first_blocks
    return payload


def register_article(title: str, filepath: str) -> dict:
    all_blocks = md_to_blocks(filepath)
    # 最初の100ブロックはページ作成時に渡し、超過分は後から追記する
    first_batch = all_blocks[:NOTION_BLOCK_LIMIT]
    remaining = all_blocks[NOTION_BLOCK_LIMIT:]

    response = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=build_headers(),
        json=build_payload(title, filepath, first_batch),
        timeout=10,
    )

    data = response.json()

    if response.status_code != 200:
        error_msg = data.get("message", "不明なエラー")
        print(f"[エラー] Notion API {response.status_code}: {error_msg}", file=sys.stderr)
        if "is a page, not a database" in error_msg:
            print(
                "\n【対処方法】\n"
                "  指定されたIDはデータベースではなくページのIDです。\n"
                "  Notionでデータベースを開き、右上「...」→「リンクをコピー」で取得したURLの\n"
                "  末尾32文字がデータベースIDです。\n"
                "  例: https://notion.so/workspace/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX?v=...\n"
                "                                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ←これがDB ID\n",
                file=sys.stderr,
            )
        sys.exit(1)

    page_id = data["id"]

    # 100ブロックを超える場合は100件ずつ追記
    for i in range(0, len(remaining), NOTION_BLOCK_LIMIT):
        append_blocks(page_id, remaining[i:i + NOTION_BLOCK_LIMIT])

    return data


def extract_title_from_file(filepath: str) -> str:
    """Markdownファイルの1行目の # 見出しからタイトルを取得する"""
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    return line.lstrip("# ").strip()
    except FileNotFoundError:
        pass
    return os.path.splitext(os.path.basename(filepath))[0]


def main():
    parser = argparse.ArgumentParser(
        description="Notionデータベースに記事情報を登録します"
    )
    parser.add_argument("--title", help="記事タイトル（省略時はファイルの # 見出しから取得）")
    parser.add_argument("--filepath", required=True, help="記事ファイルのパス")
    args = parser.parse_args()

    title = args.title or extract_title_from_file(args.filepath)
    filepath = args.filepath

    print(f"タイトル : {title}")
    print(f"ファイル : {filepath}")
    print("Notionに登録中...")

    result = register_article(title, filepath)
    page_url = result.get("url", "（URL取得不可）")
    print(f"登録完了 : {page_url}")


if __name__ == "__main__":
    main()
