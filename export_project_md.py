#!/usr/bin/env python3
"""오늘 세션 기록에서 프로젝트 관련 대화만 골라 마크다운으로 추출.

잡담·모델 질문·메타 대화(메모리, 파일 위치 등)는 제외한다.
구간 단위: 사용자 발언 하나 + 그에 대한 응답(도구 호출 포함)을 한 구간으로 보고,
사용자 발언이 제외 패턴에 걸리면 그 구간 전체를 건너뛴다.

  python3 export_project_md.py
  → ~/tx90/OMX_TX90_프로젝트_대화록_20260812.md
"""

import glob
import json
import os

PROJ = os.path.expanduser("~/.claude/projects/-home-kim-tx90")
OUT = os.path.expanduser("~/tx90/OMX_TX90_프로젝트_대화록_20260812.md")

MAX_RESULT = 2500      # 도구 출력 1건당 최대 글자
MAX_INPUT = 1200       # 도구 입력 1건당 최대 글자

# 이 패턴이 사용자 발언에 들어 있으면 그 구간은 프로젝트와 무관 → 제외
SKIP_CONTAINS = [
    "너 지금 모델",            # 모델 버전 질문
    "너 지금 여기서 우리가 대화",  # claude.ai 이관 논의
    "누구 메모리",              # 메모리 위치 질문
    "파일 어딨어",              # 파일 위치 확인
    "저장이 안된",              # PPT 저장 확인
    "몇 페이지였어",            # PPT 페이지 수
    "오늘 나눈 대화",            # 이 추출 요청 자체
]
SKIP_EXACT = {"아", "+", ""}

# 구간 판정과 무관하게 그냥 건너뛰는 표시성 발언
# (시스템이 사용자 턴으로 끼워넣는 것들 — 실제 발언이 아니므로 keep 상태를 바꾸지 않는다)
MARKERS = ("[Request interrupted", "<local-command-caveat", "<command-name>",
           "<system-reminder>", "<ide_opened_file", "[Image:",
           "<local-command-stdout", "Approach this as the design lead")


def blocks(content):
    if isinstance(content, str):
        return [("text", content)]
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                out.append(("text", b.get("text", "")))
            elif t == "tool_use":
                out.append(("tool_use", b))
            elif t == "tool_result":
                out.append(("tool_result", b))
    return out


def flatten(payload):
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
    return s if len(s) <= n else s[:n] + f"\n… (이하 {len(s)-n:,}자 생략)"


def main():
    path = max(glob.glob(os.path.join(PROJ, "*.jsonl")), key=os.path.getmtime)
    recs = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    names = {}
    for r in recs:
        msg = r.get("message")
        if isinstance(msg, dict):
            for kind, b in blocks(msg.get("content")):
                if kind == "tool_use":
                    names[b.get("id")] = b.get("name", "?")

    md = ["# OMX → TX2-90 EE Pose 전이 — 프로젝트 대화록 (2026-08-12)", ""]
    md += ["프로젝트 관련 대화만 추출한 기록입니다. 도구 호출·출력은 접힌 상태로 포함.",
           "", "---", ""]

    keep = False                 # 첫 실제 사용자 발언 전까지는 버림
    n_user = n_asst = n_tool = n_skip = 0

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
                if r["type"] == "user":
                    if any(txt.startswith(m) or m in txt[:120] for m in MARKERS):
                        continue                       # 표시성 발언, 상태 유지
                    # 새 구간 시작 → 유지/제외 판정
                    if txt in SKIP_EXACT or any(p in txt for p in SKIP_CONTAINS):
                        keep = False
                        n_skip += 1
                        continue
                    keep = True
                    n_user += 1
                    md += ["## 👤 사용자", "", txt, ""]
                elif keep:
                    n_asst += 1
                    md += ["## 🤖 Claude", "", txt, ""]

            elif kind == "tool_use" and keep:
                n_tool += 1
                inp = json.dumps(b.get("input", {}), ensure_ascii=False, indent=2)
                md += [f"**🔧 {b.get('name','?')}**", "",
                       "```json", clip(inp, MAX_INPUT), "```", ""]

            elif kind == "tool_result" and keep:
                nm = names.get(b.get("tool_use_id"), "결과")
                body = clip(flatten(b.get("content")), MAX_RESULT)
                if body.strip():
                    md += [f"<details><summary>▸ {nm} 출력</summary>", "",
                           "```", body, "```", "", "</details>", ""]

    text = "\n".join(md)
    with open(OUT, "w") as f:
        f.write(text)
    print(f"저장: {OUT}")
    print(f"  {len(text):,}자 / {text.count(chr(10)):,}줄")
    print(f"  포함: 사용자 {n_user} · Claude {n_asst} · 도구 {n_tool}  |  제외 구간 {n_skip}")


if __name__ == "__main__":
    main()
