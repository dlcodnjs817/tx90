#!/usr/bin/env python3
"""Claude Code 세션 기록(jsonl) → 읽을 수 있는 마크다운 전문.

claude.ai 에 업로드해서 이어서 작업할 때 쓴다.

  python3 export_transcript.py                      # 기본 세션, 결과 미리보기
  python3 export_transcript.py --max_result 4000    # 도구 출력 더 길게
  python3 export_transcript.py --no_tools           # 대화만
"""

import argparse
import glob
import json
import os

PROJ = os.path.expanduser("~/.claude/projects/-home-kim-tx90")


def blocks(content):
    """message.content 를 (kind, payload) 목록으로 정규화."""
    if isinstance(content, str):
        return [("text", content)]
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(("text", b.get("text", "")))
        elif t == "thinking":
            out.append(("thinking", b.get("thinking", "")))
        elif t == "tool_use":
            out.append(("tool_use", b))
        elif t == "tool_result":
            out.append(("tool_result", b))
    return out


def flatten(payload):
    """tool_result 의 content 를 문자열로."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for b in payload:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[이미지]")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return "" if payload is None else str(payload)


def clip(s, n):
    s = (s or "").rstrip()
    if len(s) <= n:
        return s
    return s[:n] + f"\n… (이하 {len(s)-n:,}자 생략)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None, help="jsonl 경로 (기본: 가장 최근)")
    ap.add_argument("--out", default=os.path.expanduser("~/tx90/대화전문_OMX_TX90.md"))
    ap.add_argument("--max_result", type=int, default=2500, help="도구 출력 1건당 최대 글자")
    ap.add_argument("--max_input", type=int, default=1200, help="도구 입력 1건당 최대 글자")
    ap.add_argument("--no_tools", action="store_true", help="도구 호출/결과 제외")
    ap.add_argument("--thinking", action="store_true", help="사고 과정도 포함")
    args = ap.parse_args()

    path = args.session or max(glob.glob(os.path.join(PROJ, "*.jsonl")),
                               key=os.path.getmtime)
    recs = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # tool_use id → 이름 (결과에 이름을 붙이려고)
    names = {}
    for r in recs:
        msg = r.get("message")
        if isinstance(msg, dict):
            for kind, b in blocks(msg.get("content")):
                if kind == "tool_use":
                    names[b.get("id")] = b.get("name", "?")

    md = ["# OMX → TX2-90 EE Pose 전이 — 작업 대화 전문", ""]
    md.append(f"- 세션: `{os.path.basename(path)}`")
    md.append(f"- 작업 폴더: `/home/kim/tx90`")
    md.append("- Claude Code 세션 기록을 마크다운으로 변환한 것입니다. "
              "도구 호출과 출력이 함께 들어 있습니다.")
    md.append("")
    md.append("---")
    md.append("")

    n_user = n_asst = n_tool = 0
    for r in recs:
        if r.get("type") not in ("user", "assistant"):
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue

        for kind, b in blocks(msg.get("content")):
            if kind == "text":
                txt = (b or "").strip()
                if not txt:
                    continue
                # 시스템이 끼워넣는 알림은 대화가 아니므로 건너뛴다
                if txt.startswith("<system-reminder>") or txt.startswith("<local-command"):
                    continue
                if r["type"] == "user":
                    n_user += 1
                    md += [f"## 👤 사용자", "", txt, ""]
                else:
                    n_asst += 1
                    md += [f"## 🤖 Claude", "", txt, ""]

            elif kind == "thinking" and args.thinking:
                md += ["<details><summary>사고 과정</summary>", "",
                       clip(b, args.max_result), "", "</details>", ""]

            elif kind == "tool_use" and not args.no_tools:
                n_tool += 1
                inp = json.dumps(b.get("input", {}), ensure_ascii=False, indent=2)
                md += [f"**🔧 {b.get('name','?')}**", "",
                       "```json", clip(inp, args.max_input), "```", ""]

            elif kind == "tool_result" and not args.no_tools:
                nm = names.get(b.get("tool_use_id"), "결과")
                body = clip(flatten(b.get("content")), args.max_result)
                if not body.strip():
                    continue
                md += [f"<details><summary>▸ {nm} 출력</summary>", "",
                       "```", body, "```", "", "</details>", ""]

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    text = "\n".join(md)
    with open(args.out, "w") as f:
        f.write(text)

    print(f"저장: {args.out}")
    print(f"  {len(text):,}자 / {len(text.encode()):,} 바이트 / {text.count(chr(10)):,}줄")
    print(f"  사용자 발언 {n_user}, Claude 응답 {n_asst}, 도구 호출 {n_tool}")


if __name__ == "__main__":
    main()
