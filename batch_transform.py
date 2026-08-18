#!/usr/bin/env python3
"""STEP 7 — 163 개 에피소드를 TX2-90 EE pose 로 일괄 변환한다.

    pos_tx90 = s * (R @ pos_omx) + t          (base_link 프레임)
    R_tx90   = R @ R_omx @ Ry(+90°)           (s 는 곱하지 않음. tool0 규약 보정 포함)

IK 는 풀지 않는다. joint 는 정책을 실제로 실행할 때 MoveIt2 가 그때그때 푼다.
163 개를 미리 IK 풀면 branch 문제가 163 배가 된다.

출력은 입력과 같은 LeRobot 구조(data/chunk-000/*.parquet + meta/)이고,
observation.state / action 의 7 개 값 배치도 동일하다:
    [x, y, z, rx, ry, rz, gripper]
달라지는 것은 기준 프레임(TX90 base_link)과 tool frame 규약(tool0, 접근축 +Z)뿐이라
학습 파이프라인에 그대로 넣을 수 있다.

  python3 batch_transform.py                       # 기본 경로로 전체 변환
  python3 batch_transform.py --drop_no_grasp       # 그리퍼가 안 닫히는 불량 제외
"""

import argparse
import glob
import json
import os
import shutil

import numpy as np
import pandas as pd
from scipy.spatial.transform import Rotation as Rot

R_TOOL = Rot.from_euler("y", np.pi / 2).as_matrix()   # OMX +X 접근 → TX90 tool0 +Z 접근
GRIP_TH = 0.459                                       # 닫힘~0.23 / 열림~0.69 의 중간

# 도달 범위 점검 (base_link 기준)
SHOULDER = np.array([0.0, 0.0, 0.478])
MAX_REACH = 0.95
COLS = ("observation.state", "action")


def transform_rows(arr, s, R, t):
    """(N,7) OMX EE → (N,7) TX90 EE. 그리퍼는 그대로 통과."""
    out = np.empty_like(arr, dtype=float)
    out[:, :3] = (s * (R @ arr[:, :3].T).T) + t
    Rm = Rot.from_euler("xyz", arr[:, 3:6]).as_matrix()
    out[:, 3:6] = Rot.from_matrix(R @ Rm @ R_TOOL).as_euler("xyz")
    out[:, 6] = arr[:, 6]
    return out


def has_grasp(grip):
    closed = grip < GRIP_TH
    tr = np.flatnonzero(np.diff(closed.astype(int)))
    return any(closed[i + 1] for i in tr) and any(not closed[i + 1] for i in tr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", default=os.path.expanduser(
        "~/omx_act_pick_and_place_v4_162_ee"))
    ap.add_argument("--transform", default=os.path.expanduser(
        "~/omx_workspace/transform.npy"))
    ap.add_argument("--out", default=os.path.expanduser("~/tx90_ee_dataset"))
    ap.add_argument("--drop_no_grasp", action="store_true",
                    help="그리퍼 개폐가 없는 불량 에피소드를 출력에서 제외")
    ap.add_argument("--exclude", type=int, nargs="*", default=[],
                    help="제외할 episode_index 목록")
    args = ap.parse_args()

    d = np.load(args.transform, allow_pickle=True).item()
    s, R, t = float(d["s"]), np.asarray(d["R"]), np.asarray(d["t"])
    print(f"[transform] {args.transform}")
    print(f"  s = {s:.6f},  t = [{t[0]:+.4f}, {t[1]:+.4f}, {t[2]:+.4f}] m")
    print(f"  residual mean {d.get('residual_mean_mm', float('nan'))*1000:.2f} mm "
          f"({d.get('n_points','?')}점)\n")

    files = sorted(glob.glob(os.path.join(args.data_dir, "**", "*.parquet"),
                             recursive=True))
    if not files:
        raise SystemExit(f"parquet 을 찾지 못했습니다: {args.data_dir}")

    out_data = os.path.join(args.out, "data", "chunk-000")
    out_meta = os.path.join(args.out, "meta")
    os.makedirs(out_data, exist_ok=True)
    os.makedirs(out_meta, exist_ok=True)

    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    n_written = n_frames = 0
    far = below = 0
    skipped, no_grasp, down_ratios = [], [], []

    for f in files:
        df = pd.read_parquet(f)
        ep = int(df["episode_index"].iloc[0])
        grip = np.stack(df["observation.state"].to_numpy())[:, 6]

        if not has_grasp(grip):
            no_grasp.append(ep)
            if args.drop_no_grasp:
                skipped.append(ep)
                continue
        if ep in args.exclude:
            skipped.append(ep)
            continue

        for c in COLS:
            arr = np.stack(df[c].to_numpy()).astype(float)
            new = transform_rows(arr, s, R, t)
            df[c] = list(new)
            if c == "observation.state":
                p = new[:, :3]
                lo = np.minimum(lo, p.min(0))
                hi = np.maximum(hi, p.max(0))
                dist = np.linalg.norm(p - SHOULDER, axis=1)
                far += int((dist > MAX_REACH).sum())
                below += int((p[:, 2] < 0.0).sum())
                appr = Rot.from_euler("xyz", new[:, 3:6]).as_matrix()[:, 2, 2]
                down_ratios.append(float((appr < -0.5).mean()))

        df.to_parquet(os.path.join(out_data, os.path.basename(f)), index=False)
        n_written += 1
        n_frames += len(df)

    # ── 메타 ──
    src_info = os.path.join(args.data_dir, "meta", "info.json")
    info = json.load(open(src_info)) if os.path.exists(src_info) else {}
    names = ["x", "y", "z", "rx", "ry", "rz", "gripper"]
    info.setdefault("features", {})
    for c in COLS:
        info["features"][c] = {"dtype": "float32", "shape": [7], "names": names}
    info["tx90_transform"] = {
        "s": s, "R": R.tolist(), "t": t.tolist(),
        "frame": "base_link",
        "ee_link": "tool0",
        "tool_fix": "Ry(+90deg)  OMX end_effector_link(+X) -> TX90 tool0(+Z)",
        "source": args.data_dir,
        "residual_mean_mm": float(d.get("residual_mean_mm", np.nan)) * 1000,
    }
    info["total_episodes"] = n_written
    info["total_frames"] = n_frames
    with open(os.path.join(out_meta, "info.json"), "w") as fp:
        json.dump(info, fp, indent=2, ensure_ascii=False)
    shutil.copy(args.transform, os.path.join(out_meta, "transform.npy"))

    # ── 요약 ──
    print(f"[출력] {args.out}")
    print(f"  parquet {n_written}개 (입력 {len(files)}개), 총 {n_frames:,} 프레임")
    if skipped:
        print(f"  제외: {skipped}")
    if no_grasp:
        tag = "제외함" if args.drop_no_grasp else "그대로 포함 — --drop_no_grasp 로 뺄 수 있음"
        print(f"  그리퍼 개폐 없는 불량 에피소드 {no_grasp} ({tag})")

    print(f"\n[sanity check]  TX90 base_link 프레임")
    for i, n in enumerate("xyz"):
        print(f"  {n}: {lo[i]:+.4f} ~ {hi[i]:+.4f} m")
    print(f"  리치 {MAX_REACH} m 초과 프레임: {far}개"
          + ("" if far == 0 else "  ← 확인 필요"))
    print(f"  바닥(z<0) 아래 프레임      : {below}개"
          + ("" if below == 0 else "  ← 확인 필요"))
    dr = np.array(down_ratios) * 100
    print(f"  그리퍼가 아래를 보는 비율  : 평균 {dr.mean():.1f}%, 최저 {dr.min():.1f}% "
          f"(에피소드 단위)")
    if far == 0 and below == 0 and dr.mean() > 90:
        print(f"\n  이상 없음. 전이학습 데이터로 쓰시면 됩니다.")


if __name__ == "__main__":
    main()
