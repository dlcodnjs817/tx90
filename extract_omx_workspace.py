#!/usr/bin/env python3
"""STEP 2 — OMX 작업영역·4 코너 추출.

전 에피소드의 EE 좌표를 합쳐서 작업영역 크기(W x H)와 바깥 4 코너를 뽑는다.
이 4 코너가 Umeyama 대응점의 OMX 쪽 값이 된다.

코너 순서 (PDF 3-2 와 동일):

    P1 ---- P2      P1 = (xmin, ymin)
    |       |       P2 = (xmax, ymin)
    P4 ---- P3      P3 = (xmax, ymax)
                    P4 = (xmin, ymax)

── 기준면(테이블 표면) 높이를 잡는 법 ──
후보가 셋인데 둘은 틀린다.
  (X) 전 프레임 z 의 하위 percentile = 21.9 mm — HOME(대기) 자세다. 163 개 에피소드가
      모두 x~0.10, y~0.00, z~0.037 에서 시작·종료해서 하위 구간을 통째로 차지한다.
  (X) grasp/release 순간의 z 평균 = 57.4 mm — 물체를 "집은" 높이지 테이블이 아니다.
      물체 두께만큼 떠 있어서, 이걸 기준면으로 쓰면 사각형 안 궤적의 17.7% 가
      기준면 아래로 최대 29 mm 파고든다.
  (O) 사각형 안에서 EE 가 도달한 최저 z = 28.2 mm — 테이블 표면에 가장 가까운 값.
      이걸 쓰면 기준면 아래로 내려가는 프레임이 0 이다.

── 사각형을 무엇으로 잡을지 ──
  --rect task  (기본) grasp/release 지점만으로 잡는다. 실제 pick&place 가
               일어나는 영역이라 TX90 에 사각형을 놓을 때 의미가 분명하다.
  --rect full  PDF 원안. 전 프레임의 1~99% 경계. HOME 자세가 포함된다.
"""

import argparse
import glob
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# 그래프 한글 깨짐 방지 (없으면 기본 폰트로 조용히 넘어간다)
# 이 시스템의 matplotlib 은 Noto CJK ttc 중 JP 페이스만 등록하는데,
# Noto Sans CJK 는 지역 공통 글리프셋이라 JP 로도 한글이 나온다.
_installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
for _f in ("Noto Sans CJK KR", "NanumGothic", "Noto Sans CJK JP", "Malgun Gothic"):
    if _f in _installed:
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

# ───────────────────────── ★ 설정 (STEP 1 결과 반영) ─────────────────────────
EE_MODE = 1                              # 1: 배열 컬럼 안에 EE 있음 / 2: 개별 컬럼

# MODE 1 이면:
EE_ARRAY_COLUMN = "observation.state"    # EE 배열 컬럼명
IDX_X, IDX_Y, IDX_Z = 0, 1, 2            # x, y, z 인덱스
IDX_GRIP = 6                             # gripper 인덱스

# MODE 2 이면:
COL_X, COL_Y, COL_Z, COL_GRIP = "ee_x", "ee_y", "ee_z", "ee_grip"
# ─────────────────────────────────────────────────────────────────────────────


def load_episodes(data_dir):
    """에피소드별 (N,4) [x,y,z,gripper] 배열 리스트를 반환."""
    files = sorted(glob.glob(os.path.join(data_dir, "**", "*.parquet"),
                             recursive=True))
    if not files:
        raise SystemExit(f"parquet 을 찾지 못했습니다: {data_dir}")

    eps = []
    for f in files:
        df = pd.read_parquet(f)
        if EE_MODE == 1:
            a = np.stack(df[EE_ARRAY_COLUMN].to_numpy())
            eps.append(a[:, [IDX_X, IDX_Y, IDX_Z, IDX_GRIP]].astype(float))
        else:
            eps.append(df[[COL_X, COL_Y, COL_Z, COL_GRIP]].to_numpy().astype(float))
    return files, eps


def grasp_events(eps, thresh):
    """그리퍼 개폐 전이를 찾아 grasp / release 지점의 xyz 를 반환."""
    pick, place, nfail = [], [], 0
    for a in eps:
        closed = a[:, 3] < thresh
        tr = np.flatnonzero(np.diff(closed.astype(int)))
        ci = [i for i in tr if closed[i + 1]]          # 닫힘 = grasp
        oi = [i for i in tr if not closed[i + 1]]      # 열림 = release
        if ci:
            pick.append(a[ci[0], :3])
        if oi:
            place.append(a[oi[-1], :3])
        if not ci or not oi:
            nfail += 1
    return np.array(pick), np.array(place), nfail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", default=os.path.expanduser(
        "~/omx_act_pick_and_place_v4_162_ee"))
    ap.add_argument("--out", default=os.path.expanduser("~/omx_workspace"))
    ap.add_argument("--rect", choices=["task", "full"], default="task",
                    help="사각형 기준: task=grasp/release 지점, full=전 프레임 percentile")
    ap.add_argument("--pct", type=float, default=1.0,
                    help="--rect full 일 때 경계 percentile (기본 1 → 1%%~99%%)")
    ap.add_argument("--grip_thresh", type=float, default=None,
                    help="그리퍼 개폐 임계값 (기본: 10/90 percentile 중간값 자동)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    files, eps = load_episodes(args.data_dir)
    allpts = np.concatenate(eps)
    x, y, z, g = allpts.T
    print(f"parquet {len(files)}개, 에피소드 {len(eps)}개, 총 {len(allpts)} 프레임")

    # ── 그리퍼 임계값 & grasp/release 검출 ──
    if args.grip_thresh is None:
        g_lo, g_hi = np.percentile(g, [10, 90])
        thresh = (g_lo + g_hi) / 2
        print(f"\n그리퍼 임계값 자동: {thresh:.3f}  "
              f"(닫힘~{g_lo:.3f} / 열림~{g_hi:.3f})")
    else:
        thresh = args.grip_thresh
        print(f"\n그리퍼 임계값 지정: {thresh:.3f}")

    pick, place, nfail = grasp_events(eps, thresh)
    if len(pick) == 0 or len(place) == 0:
        raise SystemExit("grasp/release 를 검출하지 못했습니다. --grip_thresh 를 조정하세요.")
    print(f"grasp {len(pick)}개 / release {len(place)}개 검출 (실패 {nfail}개 에피소드)")

    # ── 사각형 ──
    if args.rect == "task":
        pts = np.r_[pick[:, :2], place[:, :2]]
        xmin, xmax = pts[:, 0].min(), pts[:, 0].max()
        ymin, ymax = pts[:, 1].min(), pts[:, 1].max()
        basis = "grasp/release 지점 " + str(len(pts)) + "개의 min/max"
    else:
        lo, hi = args.pct, 100.0 - args.pct
        xmin, xmax = np.percentile(x, [lo, hi])
        ymin, ymax = np.percentile(y, [lo, hi])
        basis = f"전 프레임 {lo}%~{hi}% percentile"

    W, H = xmax - xmin, ymax - ymin

    # ── 기준면(테이블 표면) 높이 ──
    # grasp 순간의 z 는 "물체를 집은 높이"이지 테이블 표면이 아니다. 물체 두께만큼 떠 있다.
    # 그 값을 기준면으로 쓰면 궤적의 상당수가 기준면 아래로 내려가 버린다.
    # 사각형 안에서 EE 가 도달한 최저 z 가 테이블 표면에 가장 가까운 값이다.
    inrect = ((x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax))
    z_in = z[inrect]
    z_table = float(z_in.min())
    z_grasp = float(np.r_[pick[:, 2], place[:, 2]].mean())
    z_pct = float(np.percentile(z, args.pct))
    z_top = float(np.percentile(z, 100 - args.pct))
    lift = z_top - z_table

    print(f"\n[기준면 높이]  사각형 안 {inrect.sum()} 프레임 기준")
    print(f"  최저 도달 z       = {z_table*1000:+.1f} mm   ← 이 값을 사용 (테이블 표면)")
    print(f"  grasp/release 평균 = {z_grasp*1000:+.1f} mm   "
          f"물체를 집은 높이. 표면보다 {(z_grasp-z_table)*1000:.0f} mm 높다 → 기준면으로 쓰면 안 됨")
    print(f"  전 프레임 {args.pct}% percentile = {z_pct*1000:+.1f} mm   HOME 자세. 쓰지 않음")
    n_below = int((z_in < z_table).sum())
    print(f"  기준면 아래로 내려가는 프레임: {n_below}개 "
          f"(grasp 평균을 썼다면 {(z_in < z_grasp).sum()}개, 최대 "
          f"{(z_grasp-z_in.min())*1000:.0f} mm 침투)")

    corners = np.array([
        [xmin, ymin, z_table],   # P1
        [xmax, ymin, z_table],   # P2
        [xmax, ymax, z_table],   # P3
        [xmin, ymax, z_table],   # P4
    ])
    p5 = np.array([xmin, ymin, z_table + lift])

    print(f"\n[작업영역]  기준: {basis}")
    print(f"  W x H = {W*1000:.1f} x {H*1000:.1f} mm   (가로:세로 = 1 : {H/W:.3f})")
    print(f"  수직 이동폭 = {lift*1000:.1f} mm  (테이블 → 최고점 z={z_top*1000:.0f} mm)")

    print(f"\n[OMX 4 코너]  단위 m")
    for i, c in enumerate(corners, 1):
        print(f"  P{i} = [{c[0]:+.6f}, {c[1]:+.6f}, {c[2]:+.6f}]")
    print(f"\n[제안 P5]  P1 바로 위 {lift*1000:.0f} mm — TX90 에서 같이 측정 권장")
    print(f"  P5 = [{p5[0]:+.6f}, {p5[1]:+.6f}, {p5[2]:+.6f}]")

    # ── omx_corners.txt ──
    txt = os.path.join(args.out, "omx_corners.txt")
    with open(txt, "w") as f:
        f.write("# OMX 작업영역 4 코너 (+ 제안 P5)\n")
        f.write(f"# data_dir  : {args.data_dir}\n")
        f.write(f"# parquet   : {len(files)}개, 에피소드 {len(eps)}개, {len(allpts)} 프레임\n")
        f.write(f"# 사각형 기준: {basis}\n")
        f.write(f"# W x H     : {W*1000:.1f} x {H*1000:.1f} mm "
                f"(가로:세로 = 1 : {H/W:.3f})\n")
        f.write(f"# 기준면 z  : {z_table*1000:+.1f} mm "
                f"(사각형 안 최저 도달 z = 테이블 표면)\n")
        f.write(f"# 수직 이동폭: {lift*1000:.1f} mm\n\n")
        f.write("OMX_CORNERS = np.array([\n")
        for i, c in enumerate(corners, 1):
            f.write(f"    [{c[0]:+.6f}, {c[1]:+.6f}, {c[2]:+.6f}],   # P{i}\n")
        f.write("])\n\n")
        f.write("# 5 점으로 갈 경우 (Z 방향을 실측으로 고정 — 권장)\n")
        f.write(f"# P5 = [{p5[0]:+.6f}, {p5[1]:+.6f}, {p5[2]:+.6f}]"
                f"   # P1 위 {lift*1000:.0f} mm\n")
    print(f"\n저장: {txt}")

    # ── 그림 ──
    fig, axes = plt.subplots(1, 4, figsize=(21, 5))

    ax = axes[0]
    ax.scatter(x, y, s=0.4, alpha=0.08, c="0.6", linewidths=0, label="전체 궤적")
    ax.scatter(*pick[:, :2].T, s=14, c="tab:red", label=f"grasp ({len(pick)})")
    ax.scatter(*place[:, :2].T, s=14, c="tab:blue", label=f"release ({len(place)})")
    rect = np.vstack([corners[:, :2], corners[0, :2]])
    ax.plot(rect[:, 0], rect[:, 1], "r-", lw=2)
    for i, c in enumerate(corners, 1):
        ax.plot(*c[:2], "ro", ms=7)
        ax.annotate(f"P{i}", c[:2], textcoords="offset points",
                    xytext=(8, 8), color="red", fontweight="bold")
    ax.set_xlabel("x [m]"); ax.set_ylabel("y [m]")
    ax.set_title(f"XY (top view)   W x H = {W*1000:.0f} x {H*1000:.0f} mm")
    ax.set_aspect("equal"); ax.grid(alpha=0.3); ax.legend(fontsize=8, loc="best")

    ax = axes[1]
    ax.scatter(x, z, s=0.4, alpha=0.08, c="0.6", linewidths=0)
    ax.scatter(pick[:, 0], pick[:, 2], s=14, c="tab:red")
    ax.scatter(place[:, 0], place[:, 2], s=14, c="tab:blue")
    ax.axhline(z_table, color="r", ls="--", lw=1.5,
               label=f"기준면 z = {z_table*1000:.0f} mm")
    ax.axhline(z_pct, color="0.4", ls=":", lw=1.5,
               label=f"{args.pct}% percentile = {z_pct*1000:.0f} mm (HOME)")
    ax.set_xlabel("x [m]"); ax.set_ylabel("z [m]")
    ax.set_title("XZ (side view)"); ax.legend(fontsize=8); ax.grid(alpha=0.3)

    ax = axes[2]
    ax.hist(z * 1000, bins=120, color="0.6")
    ax.hist(np.r_[pick[:, 2], place[:, 2]] * 1000, bins=40, color="tab:red",
            alpha=0.85, label="grasp/release (집은 높이)")
    ax.axvline(z_table * 1000, color="r", ls="--", lw=1.5)
    ax.axvline(z_pct * 1000, color="0.3", ls=":", lw=1.5)
    ax.set_xlabel("z [mm]"); ax.set_ylabel("frames")
    ax.set_title("z 분포 — 기준면/집은높이/HOME")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)

    ax = axes[3]
    ax.hist(g, bins=120, color="tab:green")
    ax.axvline(thresh, color="r", ls="--", lw=1.5, label=f"임계값 {thresh:.3f}")
    ax.set_xlabel("gripper [rad]"); ax.set_ylabel("frames")
    ax.set_title("그리퍼 값 분포 (개폐 판정)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)

    fig.tight_layout()
    png = os.path.join(args.out, "omx_workspace.png")
    fig.savefig(png, dpi=130)
    print(f"저장: {png}")

    print(f"\n── STEP 3 에서 할 일 ──")
    print(f"  TX90 테이블에 {W*1000:.0f} x {H*1000:.0f} mm 사각형 "
          f"(가로:세로 1:{H/W:.3f}) 을 잡고 P1~P4 를 같은 순서로 측정")
    print(f"  4 코너는 테이블 표면 높이, P5 는 그 위 {lift*1000:.0f} mm")
    print(f"  OMX 와 TX90 사각형 크기를 같게 잡으므로 STEP 4 의 s 는 1.0 근처가 정상")


if __name__ == "__main__":
    main()
