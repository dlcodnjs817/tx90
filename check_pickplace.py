#!/usr/bin/env python3
"""변환된 TX90 궤적에 pick & place 패턴이 실제로 있는지 그림과 숫자로 확인한다.

RViz 에서 눈으로 판단하기 어려울 때 쓴다. ROS 없이 host 에서 돈다.

  python3 check_pickplace.py                 # 에피소드 0, 80, 161
  python3 check_pickplace.py --episodes 3 7  # 원하는 에피소드
"""

import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.spatial.transform import Rotation as Rot

_installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
for _f in ("Noto Sans CJK KR", "NanumGothic", "Noto Sans CJK JP", "Malgun Gothic"):
    if _f in _installed:
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

DATA = os.path.expanduser("~/omx_act_pick_and_place_v4_162_ee/data/chunk-000")
GRIP_TH = 0.459                      # 닫힘~0.23 / 열림~0.69 의 중간
R_TOOL = Rot.from_euler("y", np.pi / 2).as_matrix()


def load(ep, s, R, t):
    df = pd.read_parquet(os.path.join(DATA, f"episode_{ep:06d}.parquet"))
    a = np.stack(df["action"].to_numpy())
    ts = df["timestamp"].to_numpy().astype(float)
    pos = (s * (R @ a[:, :3].T).T) + t
    appr = np.array([(R @ Rot.from_euler("xyz", r).as_matrix() @ R_TOOL)[:, 2][2]
                     for r in a[:, 3:6]])
    return ts, pos, appr, a[:, 6]


def events(grip):
    """그리퍼 닫힘/열림 전이 프레임 인덱스."""
    closed = grip < GRIP_TH
    tr = np.flatnonzero(np.diff(closed.astype(int)))
    return ([i for i in tr if closed[i + 1]],       # grasp
            [i for i in tr if not closed[i + 1]])   # release


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, nargs="+", default=[0, 80, 161])
    ap.add_argument("--transform", default=os.path.expanduser(
        "~/omx_workspace/transform.npy"))
    ap.add_argument("--out", default=os.path.expanduser("~/omx_workspace/pickplace_check.png"))
    args = ap.parse_args()

    d = np.load(args.transform, allow_pickle=True).item()
    s, R, t = float(d["s"]), np.asarray(d["R"]), np.asarray(d["t"])

    n = len(args.episodes)
    fig, axes = plt.subplots(n, 3, figsize=(16, 3.6 * n), squeeze=False)

    for row, ep in enumerate(args.episodes):
        ts, pos, appr, grip = load(ep, s, R, t)
        gi, oi = events(grip)
        z = pos[:, 2] * 1000

        print(f"\n{'='*58}\n에피소드 {ep}  —  {len(ts)} 프레임, {ts[-1]-ts[0]:.1f}초")
        print(f"  z 범위 {z.min():.0f} ~ {z.max():.0f} mm  (수직 이동 {z.max()-z.min():.0f} mm)")
        print(f"  그리퍼가 아래를 보는 비율 {100*(appr<-0.5).mean():.1f}%")
        for i in gi:
            print(f"  {ts[i]:5.1f}초  GRASP   z={z[i]:6.0f} mm  "
                  f"xy=[{pos[i,0]*1000:.0f}, {pos[i,1]*1000:.0f}]")
        for i in oi:
            print(f"  {ts[i]:5.1f}초  RELEASE z={z[i]:6.0f} mm  "
                  f"xy=[{pos[i,0]*1000:.0f}, {pos[i,1]*1000:.0f}]")
        if gi and oi:
            a, b = gi[0], oi[-1]
            lift = z[a:b + 1].max() - z[a]
            print(f"  → 집은 뒤 {lift:.0f} mm 들어올려 "
                  f"{np.linalg.norm(pos[b,:2]-pos[a,:2])*1000:.0f} mm 이동 후 놓음")

        # 1) z(t) — pick&place 의 하강·상승 구조
        ax = axes[row][0]
        ax.plot(ts, z, color="tab:blue", lw=1.6)
        for i in gi:
            ax.axvline(ts[i], color="tab:red", lw=1.4)
            ax.annotate("집기", (ts[i], ax.get_ylim()[1]), color="tab:red",
                        fontsize=9, ha="center", va="top")
        for i in oi:
            ax.axvline(ts[i], color="tab:green", lw=1.4, ls="--")
            ax.annotate("놓기", (ts[i], ax.get_ylim()[1]), color="tab:green",
                        fontsize=9, ha="center", va="top")
        ax.set_xlabel("시간 [s]"); ax.set_ylabel("EE 높이 z [mm]")
        ax.set_title(f"ep {ep} — 높이 변화 (내려감→집기→올림→놓기)")
        ax.grid(alpha=0.3)

        # 2) XY 위에서 본 경로
        ax = axes[row][1]
        ax.plot(pos[:, 0]*1000, pos[:, 1]*1000, color="0.55", lw=1.2)
        ax.scatter(pos[0, 0]*1000, pos[0, 1]*1000, s=60, c="k", marker="s", label="시작")
        for i in gi:
            ax.scatter(pos[i, 0]*1000, pos[i, 1]*1000, s=90, c="tab:red", label="집기")
        for i in oi:
            ax.scatter(pos[i, 0]*1000, pos[i, 1]*1000, s=90, c="tab:green", label="놓기")
        ax.set_xlabel("x [mm]"); ax.set_ylabel("y [mm]")
        ax.set_title("위에서 본 경로 (TX90 base_link)")
        ax.set_aspect("equal"); ax.grid(alpha=0.3)
        h, l = ax.get_legend_handles_labels()
        ax.legend(dict(zip(l, h)).values(), dict(zip(l, h)).keys(), fontsize=8)

        # 3) 그리퍼 방향 — -1 이면 정확히 수직 아래
        ax = axes[row][2]
        ax.plot(ts, appr, color="tab:purple", lw=1.6)
        ax.axhline(-1, color="0.4", ls=":", lw=1.2)
        ax.axhline(-0.5, color="tab:orange", ls="--", lw=1.2)
        ax.fill_between(ts, -1.05, -0.5, color="tab:green", alpha=0.10)
        ax.set_ylim(-1.05, 1.05)
        ax.set_xlabel("시간 [s]"); ax.set_ylabel("접근축 z성분")
        ax.set_title("그리퍼 방향 (-1 = 수직 아래, 초록 = 아래를 봄)")
        ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"\n저장: {args.out}")


if __name__ == "__main__":
    main()
