#!/usr/bin/env python3
"""fix_euler_wrap.py — 오일러 wrap 불연속 수정: v4 → v5 데이터셋

문제 (2026-08-13 발견)
  tx90_act_pick_and_place_v4_162_ee 의 rx·rz 는 [-pi, pi] 로 저장돼 있는데
  실제 자세가 ±pi(그리퍼 수직 아래) 근처에 몰려 있어 프레임 사이에 ±pi 를
  넘나드는 ~2pi 점프가 생긴다 (rz: 159/159 에피소드 437회, rx: 106/159 289회).
  MSE 로 학습하는 ACT 는 이 불연속을 평균 내서, ±pi 근처에서 rz 를 -1.6 같은
  중간값으로 예측한다 (10k 체크포인트에서 자세오차 90퍼센타일 87°로 실측).

수정
  rx, rz 를 mod 2pi 로 [0, 2pi) 에 재매핑한다. 데이터가 pi 중심에 몰려 있고
  0 근처 프레임이 없어서 (|rz|<1.0 이 0개) 재매핑 후 불연속이 완전히 사라진다
  (전 에피소드 프레임간 최대 변화 0.119 rad 실측). ry 는 점프가 없어 그대로.
  실행(replay) 쪽은 cos/sin 만 쓰므로 각도 범위가 [0,2pi) 여도 수정이 필요 없다.

하는 일
  1. data/*.parquet 의 observation.state / action 의 rx(3), rz(5) 를 mod 2pi
  2. 영상 심볼릭 링크를 원본 타깃으로 다시 생성
  3. episodes_stats 의 state/action 통계 재계산 (나머지는 그대로)
  4. info.json 에 수정 내역 기록

  python3 fix_euler_wrap.py          # 기본 경로 v4 → v5
"""

import argparse
import glob
import json
import os
import shutil

import numpy as np
import pandas as pd

COLS = ("observation.state", "action")
CHUNK = "chunk-000"
TWO_PI = 2.0 * np.pi


def remap(arr):
    out = arr.astype(float).copy()
    out[:, 3] = np.mod(out[:, 3], TWO_PI)   # rx
    out[:, 5] = np.mod(out[:, 5], TWO_PI)   # rz
    return out


def stats_of(a):
    a = np.asarray(a, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    return {"min": a.min(0).tolist(), "max": a.max(0).tolist(),
            "mean": a.mean(0).tolist(), "std": a.std(0).tolist(),
            "count": [len(a)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/root/.cache/huggingface/lerobot/dlcodnjs/"
                                     "tx90_act_pick_and_place_v4_162_ee")
    ap.add_argument("--out", default="/root/.cache/huggingface/lerobot/dlcodnjs/"
                                     "tx90_act_pick_and_place_v5_ee")
    args = ap.parse_args()

    if os.path.isdir(args.out):
        # 이전 빌드가 남아 있으면 높은 번호 파일이 오염을 일으킨다 (v4 조립 때 배운 것)
        print(f"[정리] 기존 출력 삭제: {args.out}")
        shutil.rmtree(args.out)

    out_data = os.path.join(args.out, "data", CHUNK)
    out_meta = os.path.join(args.out, "meta")
    os.makedirs(out_data, exist_ok=True)
    os.makedirs(out_meta, exist_ok=True)

    # ── 1. parquet 재매핑 ──
    files = sorted(glob.glob(os.path.join(args.src, "data", CHUNK, "*.parquet")))
    print(f"[데이터] {len(files)}개 parquet")
    worst = 0.0
    new_stats_by_ep = {}
    for f in files:
        df = pd.read_parquet(f)
        ep = int(df["episode_index"].iloc[0])
        st = {}
        for c in COLS:
            new = remap(np.stack(df[c].to_numpy()))
            df[c] = list(new.astype(np.float32))
            st[c] = stats_of(new)
            if c == "action":
                j = np.abs(np.diff(new[:, [3, 5]], axis=0))
                worst = max(worst, float(j.max()) if len(j) else 0.0)
        new_stats_by_ep[ep] = st
        df.to_parquet(os.path.join(out_data, os.path.basename(f)), index=False)
    print(f"  재매핑 후 rx/rz 프레임간 최대 변화: {worst:.3f} rad "
          f"({'OK' if worst < 1.0 else '점프 잔존 — 확인 필요!'})")

    # ── 2. 영상 링크 재생성 ──
    n_vid = 0
    for src_dir, _, names in os.walk(os.path.join(args.src, "videos")):
        rel = os.path.relpath(src_dir, args.src)
        dst_dir = os.path.join(args.out, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for n in names:
            src_v = os.path.join(src_dir, n)
            target = os.path.realpath(src_v)          # 심볼릭 링크면 원본으로
            dst_v = os.path.join(dst_dir, n)
            os.symlink(os.path.relpath(target, dst_dir), dst_v)
            n_vid += 1
    print(f"[영상] 심볼릭 링크 {n_vid}개 재생성")

    # ── 3. meta ──
    for n in ("episodes.jsonl", "tasks.jsonl"):
        shutil.copy(os.path.join(args.src, "meta", n), os.path.join(out_meta, n))
    src_stats = os.path.join(args.src, "meta", "episodes_stats.jsonl")
    with open(src_stats) as fin, \
         open(os.path.join(out_meta, "episodes_stats.jsonl"), "w") as fout:
        for line in fin:
            e = json.loads(line)
            ep = e["episode_index"]
            e["stats"].update(new_stats_by_ep[ep])
            fout.write(json.dumps(e, ensure_ascii=False) + "\n")

    info = json.load(open(os.path.join(args.src, "meta", "info.json")))
    info["euler_wrap_fix"] = {
        "date": "2026-08-13",
        "what": "rx, rz remapped to [0, 2pi) via mod 2pi (both observation.state and action)",
        "why": "values clustered near +-pi caused ~2pi jumps between frames "
               "(rz 159/159 eps, rx 106/159); MSE-trained policy averaged across "
               "the discontinuity",
        "source_dataset": args.src,
    }
    with open(os.path.join(out_meta, "info.json"), "w") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    t_npy = os.path.join(args.src, "meta", "transform.npy")
    if os.path.exists(t_npy):
        shutil.copy(t_npy, os.path.join(out_meta, "transform.npy"))

    print(f"\n[출력] {args.out}")
    print(f"  에피소드 {info['total_episodes']}개, {info['total_frames']:,} 프레임")
    print("  다음: lerobot 로드 검증 후 이 repo_id 로 재학습")


if __name__ == "__main__":
    main()
