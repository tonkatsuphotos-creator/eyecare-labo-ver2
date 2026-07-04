#!/usr/bin/env python3
"""
季節感を考慮してネタストックを10件生成し、Notionに登録するスクリプト

Usage:
    python tools/generate_topics.py
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

if not TOKEN:
    print("[エラー] NOTION_TOKEN が未設定です。.env ファイルを確認してください。", file=sys.stderr)
    sys.exit(1)
if not DATABASE_ID:
    print("[エラー] NOTION_DATABASE_ID が未設定です。.env ファイルを確認してください。", file=sys.stderr)
    sys.exit(1)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PROJECT_ROOT = Path(__file__).parent.parent

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から自動取得
CLAUDE_MODEL = "claude-sonnet-5"


# ── Notion API ────────────────────────────────────────────


def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def fetch_existing_stock_titles() -> list:
    """現在ネタストック状態のタイトル一覧を取得（重複チェック用）"""
    titles = []
    cursor = None
    while True:
        body = {
            "filter": {"property": "ステータス", "select": {"equals": "ネタストック"}},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{DATABASE_ID}/query",
            headers=notion_headers(),
            json=body,
            timeout=10,
        )
        data = resp.json()
        for page in data.get("results", []):
            title_items = page["properties"].get("名前", {}).get("title", [])
            if title_items:
                titles.append(title_items[0]["plain_text"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return titles


def register_topic(title: str, category: str, pattern: str, target: str) -> str:
    """Notionにネタストックを1件登録してページIDを返す"""
    resp = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=notion_headers(),
        json={
            "parent": {"database_id": DATABASE_ID},
            "properties": {
                "名前": {"title": [{"text": {"content": title}}]},
                "ステータス": {"select": {"name": "ネタストック"}},
                "テーマカテゴリ": {"select": {"name": category}},
                "切り口パターン": {"select": {"name": pattern}},
                "ターゲット読者": {"select": {"name": target}},
            },
        },
        timeout=10,
    )
    if resp.status_code != 200:
        msg = resp.json().get("message", "不明なエラー")
        print(f"[エラー] Notion登録失敗（{title}）: {msg}", file=sys.stderr)
        sys.exit(1)
    return resp.json()["id"]


# ── ネタ生成 ──────────────────────────────────────────────


def build_prompt(current_month: str, next_month: str, past_themes_text: str, existing_titles: list) -> str:
    existing_str = "\n".join(f"- {t}" for t in existing_titles) if existing_titles else "（なし）"
    return f"""あなたはアイケアLaBo四ツ谷店（眼精疲労・老眼ケア専門の整体店）のコンテンツ担当です。
GBP（Googleビジネスプロフィール）投稿用の記事ネタを10件生成してください。

## 条件
- 当月：{current_month}、翌月：{next_month} の季節感・生活シーンを考慮する
- 過去記事・既存ネタストックと重複しないテーマにする
- 以下の選択肢から1つずつ選び、10件全体でバランスよく組み合わせる（同じ組み合わせの繰り返しはNG）

### テーマカテゴリ（いずれか1つ）
眼精疲労 / 老眼 / 子どもの視力 / 肩こり・頭痛連動 / ドライアイ / 季節テーマ

### 切り口パターン（いずれか1つ）
症状起点 / 年代起点 / シーン起点 / 季節起点 / 行動起点

### ターゲット読者（いずれか1つ）
30代デスクワーカー / 40代老眼世代 / 50代以上 / 親御さん向け / 女性全般

## 過去の使用済みテーマ・構成（これらと被らないこと）
{past_themes_text}

## 既存のネタストック（これらと被らないこと）
{existing_str}

## 出力形式
JSONの配列のみを出力する。説明文・前置き・コードブロック記号（```）は一切出力しない。

[
  {{
    "title": "記事タイトル（読者の悩みを表す具体的な文章、30字以内）",
    "category": "テーマカテゴリ",
    "pattern": "切り口パターン",
    "target": "ターゲット読者"
  }}
]
"""


def generate_topics_with_claude(prompt: str) -> list:
    """Anthropic APIでネタ10件を生成してリストで返す"""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = response.usage
    print(f"  [トークン] input={usage.input_tokens} output={usage.output_tokens}")

    output = "".join(b.text for b in response.content if b.type == "text").strip()
    output = re.sub(r"```[^\n]*\n?", "", output).strip()

    match = re.search(r"\[.*\]", output, re.DOTALL)
    if not match:
        print(f"[エラー] JSON配列が見つかりません:\n{output}", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[エラー] JSONパース失敗: {e}\n{match.group()}", file=sys.stderr)
        sys.exit(1)


# ── メイン ────────────────────────────────────────────────


def main():
    print("=" * 40)
    print("  ネタストック生成フロー開始")
    print("=" * 40)

    now = datetime.now()
    current_month = f"{now.year}年{now.month}月"
    next_month_num = now.month % 12 + 1
    next_month_year = now.year if next_month_num != 1 else now.year + 1
    next_month = f"{next_month_year}年{next_month_num}月"
    print(f"\n  当月: {current_month}  翌月: {next_month}")

    past_themes_path = PROJECT_ROOT / "references" / "past_themes.md"
    past_themes_text = past_themes_path.read_text(encoding="utf-8") if past_themes_path.exists() else "（なし）"

    print("\n[1/3] 既存のネタストックを確認中...")
    existing_titles = fetch_existing_stock_titles()
    print(f"  既存ネタストック数: {len(existing_titles)}件")

    print("\n[2/3] Claude APIでネタを生成中...")
    prompt = build_prompt(current_month, next_month, past_themes_text, existing_titles)
    topics = generate_topics_with_claude(prompt)
    print(f"  生成件数: {len(topics)}件")

    print("\n[3/3] Notionに登録中...")
    registered = 0
    for i, topic in enumerate(topics, 1):
        title = topic.get("title", "").strip()
        category = topic.get("category", "").strip()
        pattern = topic.get("pattern", "").strip()
        target = topic.get("target", "").strip()

        if not title:
            print(f"  [{i}] タイトルが空のためスキップ")
            continue

        register_topic(title, category, pattern, target)
        print(f"  [{i}] 登録: {title}")
        registered += 1

    print("\n" + "=" * 40)
    print(f"  完了（{registered}件登録）")
    print("=" * 40)


if __name__ == "__main__":
    main()
