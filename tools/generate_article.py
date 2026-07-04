#!/usr/bin/env python3
"""
ネタストック記事を1件取得し、Claude Codeで記事を生成してNotionに登録するスクリプト

Usage:
    python tools/generate_article.py

フロー:
    1. NotionDBからステータス「ネタストック」の記事を並び順1位で取得
    2. CLAUDE.md・references/を参照してClaude APIで記事生成
    3. articles/フォルダにMarkdownで保存
    4. Notionページに記事本文を貼り付け
    5. ステータスを「生成済」に更新
"""

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
NOTION_BLOCK_LIMIT = 100

PROJECT_ROOT = Path(__file__).parent.parent

GITHUB_OWNER = os.environ.get("GH_OWNER", "Rplus-shop")
GITHUB_REPO = os.environ.get("GH_REPO", "eyecare-labo")
GITHUB_ARTICLE_BASE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/blob/main/articles"

ARTICLE_FOOTER = """\
---
アイケアLaBo四ツ谷店
〒160-0004
東京都新宿区四谷1丁目18
Belle四谷4F

#眼の整体 #眼精疲労 #老眼 #近視
#目の疲れ #スマホ疲れ
#PC疲れ #目のケア #血流改善 #四ツ谷 #新宿
#四ツ谷サロン #駅近サロン #頭痛 #肩こり
---"""

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から自動取得
CLAUDE_MODEL = "claude-sonnet-5"


# ── Notion API ────────────────────────────────────────────


def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def fetch_next_stocks(n: int = 10) -> list:
    """ネタストックを並び順の昇順でn件取得する"""
    resp = requests.post(
        f"{NOTION_API_BASE}/databases/{DATABASE_ID}/query",
        headers=notion_headers(),
        json={
            "filter": {"property": "ステータス", "select": {"equals": "ネタストック"}},
            "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
            "page_size": n,
        },
        timeout=10,
    )
    results = resp.json().get("results", [])
    if not results:
        print("ネタストックの記事が見つかりません。終了します。")
        sys.exit(0)
    return results


def extract_props(page: dict) -> dict:
    props = page["properties"]

    def title_text(prop):
        items = prop.get("title") or []
        return items[0]["plain_text"] if items else ""

    def select_name(prop):
        s = prop.get("select")
        return s["name"] if s else ""

    return {
        "id": page["id"],
        "title": title_text(props["名前"]),
        "category": select_name(props["テーマカテゴリ"]),
        "pattern": select_name(props["切り口パターン"]),
        "target": select_name(props["ターゲット読者"]),
    }


def update_notion_status_and_link(page_id: str, filepath: str) -> None:
    article_url = f"{GITHUB_ARTICLE_BASE}/{Path(filepath).name}"
    resp = requests.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=notion_headers(),
        json={
            "properties": {
                "ステータス": {"select": {"name": "生成済"}},
                "生成記事リンク": {"url": article_url},
            }
        },
        timeout=10,
    )
    if resp.status_code != 200:
        msg = resp.json().get("message", "不明なエラー")
        print(f"[エラー] Notionプロパティ更新失敗: {msg}", file=sys.stderr)
        sys.exit(1)


def append_blocks_to_page(page_id: str, blocks: list) -> None:
    for i in range(0, len(blocks), NOTION_BLOCK_LIMIT):
        resp = requests.patch(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=notion_headers(),
            json={"children": blocks[i:i + NOTION_BLOCK_LIMIT]},
            timeout=10,
        )
        if resp.status_code != 200:
            msg = resp.json().get("message", "不明なエラー")
            print(f"[エラー] ブロック追記失敗: {msg}", file=sys.stderr)
            sys.exit(1)


# ── Markdown → Notion ブロック変換 ────────────────────────


def parse_inline(text: str) -> list:
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


def md_to_blocks(content: str) -> list:
    blocks = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        if re.match(r"^# [^#]", stripped):
            blocks.append({"object": "block", "type": "heading_1",
                           "heading_1": {"rich_text": parse_inline(stripped[2:])}})
            continue
        if stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                           "heading_2": {"rich_text": parse_inline(stripped[3:])}})
            continue
        if stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": parse_inline(stripped[4:])}})
            continue
        m = re.match(r"^\d+\.\s+(.*)", stripped)
        if m:
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": parse_inline(m.group(1))}})
            continue
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": parse_inline(stripped)}})
    return blocks


# ── 記事生成 ──────────────────────────────────────────────


def build_system_blocks() -> list:
    """CLAUDE.md・referencesなど、記事が変わっても不変の部分（キャッシュ対象）"""
    claude_md = (PROJECT_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    refs_text = ""
    for ref_file in sorted((PROJECT_ROOT / "references").glob("*.md")):
        content = ref_file.read_text(encoding="utf-8")
        refs_text += f"\n\n=== {ref_file.name} ===\n{content}"

    static_text = f"""以下の条件と参照ファイルをすべて守って、GBPコラム記事の本文のみを生成してください。

## プロジェクトルール（CLAUDE.md）
{claude_md}

## 参照ファイル（トンマナ・禁止表現・テンプレート・過去記事）
{refs_text}

## 差別化ルール（必ず守ること）
- past_themes.md の「使用済みの例え話」に記載されたものは使わない（カメラ、握りしめた手 など）
- past_themes.md の「使用済みの導入パターン」と異なるシーン・書き出しにする
- past_themes.md の「使用済みの構成パターン」と同じH2見出し構成を繰り返さない
- 問題提起フレーズも past_themes.md に記載済みのものは避ける

## 出力ルール
- Markdownの本文のみを出力する
- 説明文・前置き・コメント・コードブロック記号（```）は一切出力しない
- 冒頭の定型文から始め、末尾の免責文で終わること
"""
    return [{
        "type": "text",
        "text": static_text,
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]


def generate_article_with_claude(system_blocks: list, meta: dict) -> str:
    """Anthropic APIを使って記事を生成する（記事ごとに変わる部分のみ送信）"""
    current_month = datetime.now().strftime("%Y年%-m月")
    user_prompt = f"""## 今回の記事情報
- 記事タイトル：{meta['title']}
- テーマカテゴリ：{meta['category']}
- 切り口パターン：{meta['pattern']}
- ターゲット読者：{meta['target']}
- 投稿月：{current_month}
"""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_blocks,
        messages=[{"role": "user", "content": user_prompt}],
    )
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0)
    print(f"  [トークン] input={usage.input_tokens} "
          f"cache_read={cache_read} cache_write={cache_write} "
          f"output={usage.output_tokens}")

    return "".join(b.text for b in response.content if b.type == "text").strip()


def clean_output(text: str) -> str:
    """Claude出力から記事本文のみを抽出する。
    冒頭定型文から始まり、末尾免責文で終わる範囲を切り出す。
    """
    text = re.sub(r"```[^\n]*\n?", "", text)

    start_marker = "こんにちは！新宿区"
    start_idx = text.find(start_marker)
    if start_idx != -1:
        text = text[start_idx:]

    end_marker = "専門家や医師の診断を受けてください。"
    end_idx = text.find(end_marker)
    if end_idx != -1:
        text = text[: end_idx + len(end_marker)]

    return text.strip()


# ── ファイル保存 ──────────────────────────────────────────


def save_article(title: str, content: str) -> str:
    """articles/に保存してプロジェクトルートからの相対パスを返す"""
    safe_title = re.sub(r'[\\/:*?"<>|　\s]', "_", title)[:40]
    date_prefix = datetime.now().strftime("%Y%m")
    filename = f"{date_prefix}_{safe_title}.md"
    filepath = PROJECT_ROOT / "articles" / filename
    filepath.write_text(content, encoding="utf-8")
    return str(filepath.relative_to(PROJECT_ROOT))


# ── メイン ────────────────────────────────────────────────


def main():
    print("=" * 40)
    print("  記事生成フロー開始（10件）")
    print("=" * 40)

    # Step 1: Notionからネタストックを10件取得
    print("\n[1/5] Notionからネタストックを10件取得中...")
    pages = fetch_next_stocks(10)
    print(f"  取得件数: {len(pages)}件")

    # CLAUDE.md・referencesは記事間で不変のため、ループの外で1回だけ構築（キャッシュ再利用のため）
    system_blocks = build_system_blocks()

    for idx, page in enumerate(pages, 1):
        meta = extract_props(page)
        print(f"\n{'='*40}")
        print(f"  記事 {idx}/{len(pages)}: {meta['title']}")
        print(f"{'='*40}")
        print(f"  カテゴリ   : {meta['category']}")
        print(f"  切り口     : {meta['pattern']}")
        print(f"  ターゲット : {meta['target']}")

        # Step 2: Claude APIで記事生成
        print(f"\n  [2/5] Claude APIで記事を生成中...")
        raw_output = generate_article_with_claude(system_blocks, meta)
        article_content = clean_output(raw_output) + "\n\n" + ARTICLE_FOOTER
        print(f"  生成文字数 : {len(article_content)}字")

        # Step 3: Markdownファイルに保存
        print(f"\n  [3/5] Markdownファイルを保存中...")
        filepath = save_article(meta["title"], article_content)
        print(f"  保存先     : {filepath}")

        # Step 4: Notionページに本文を貼り付け
        print(f"\n  [4/5] Notionページに本文を貼り付け中...")
        blocks = md_to_blocks(article_content)
        append_blocks_to_page(meta["id"], blocks)
        print(f"  ブロック数 : {len(blocks)}")

        # Step 5: ステータスを「生成済」に更新
        print(f"\n  [5/5] ステータスを「生成済」に更新中...")
        update_notion_status_and_link(meta["id"], filepath)
        print(f"  完了: {filepath}")

    print("\n" + "=" * 40)
    print(f"  全件完了（{len(pages)}本）")
    print("=" * 40)


if __name__ == "__main__":
    main()
