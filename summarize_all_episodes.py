#!/usr/bin/env python3
"""162개 에피소드 전체의 pick & place 품질 요약 그래프.

check_pickplace.py 가 개별 에피소드 3개를 자세히 보는 것이라면,
이 스크립트는 전체 분포와 이상치를 한 장에 보여준다.

  python3 summarize_all_episodes.py
  → ~/omx_workspace/all_episodes_summary.png
"""

import glob
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_installed = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
for _f in ("Noto Sans CJK KR", "NanumGothic", "Noto Sans CJK JP", "Malgun Gothic"):
    if _f in _installed:
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

DATA = os.path.expanduser("~/tx90_ee_dataset/data/chunk-000")
OUT = os.path.expanduser("~/omx_workspace/all_episodes_summary.png")
GRIP_TH = 0.459
ZTAB = 334.1        # TX90 테이블면 (base_link, mm)
# 실측 사각형 (base_link XY, mm)
RECT_X = (611.8, 852.6)
RECT_Y = (-173.7, 187.2)


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.parquet")))
    zcurves, tg_n, tr_n = [], [], []
    rows = []          # ep, n_grasp, n_release, zg, zr, lift, move, dur
    pick_xy, place_xy = [], []

    for f in files:
        df = pd.read_parquet(f)
        a = np.stack(df["observation.state"].to_numpy()).astype(float)
        ts = df["timestamp"].to_numpy().astype(float)
        ep = int(df["episode_index"].iloc[0])
        z = a[:, 2] * 1000
        closed = a[:, 6] < GRIP_TH
        tr = np.flatnonzero(np.diff(closed.astype(int)))
        gi = [i for i in tr if closed[i + 1]]
        oi = [i for i in tr if not closed[i + 1]]

        # 시간 정규화 z 곡선 (전 에피소드 공통 구조 확인용)
        u = np.linspace(0, 1, 100)
        zcurves.append(np.interp(u, (ts - ts[0]) / (ts[-1] - ts[0]), z - ZTAB))

        if not gi or not oi:
            rows.append((ep, 0, 0, np.nan, np.nan, np.nan, np.nan, ts[-1] - ts[0]))
            continue
        g0, r1 = gi[0], oi[-1]
        rows.append((ep, len(gi), len(oi),
                     z[g0] - ZTAB, z[r1] - ZTAB,
                     z[g0:r1 + 1].max() - z[g0],
                     np.linalg.norm(a[r1, :2] - a[g0, :2]) * 1000,
                     ts[-1] - ts[0]))
        pick_xy.append(a[g0, :2] * 1000)
        place_xy.append(a[r1, :2] * 1000)
        T = ts[-1] - ts[0]
        tg_n.append((ts[g0] - ts[0]) / T)
        tr_n.append((ts[r1] - ts[0]) / T)

    r = np.array(rows, dtype=float)
    valid = (r[:, 1] > 0) & (r[:, 2] > 0)
    clean = valid & (r[:, 1] == 1) & (r[:, 2] == 1)
    retry = valid & ((r[:, 1] > 1) | (r[:, 2] > 1))
    v = r[valid]
    pick_xy, place_xy = np.array(pick_xy), np.array(place_xy)
    Z = np.array(zcurves)

    # 의심 이상치: 공중에서 놓았고 거의 안 옮김
    susp = valid & (r[:, 4] > 100) & (r[:, 6] < 80)

    print(f"전체 {len(r)}개 / 이벤트 검출 {valid.sum()}개 "
          f"(깨끗 {clean.sum()}, 재시도 {retry.sum()}, 무집기 {(~valid).sum()})")
    print("\n[의심 에피소드 상세]")
    for row in r[susp]:
        print(f"  ep{int(row[0]):3d}: 집기 {int(row[1])}회, "
              f"집기높이 {row[3]:.0f}mm, 놓기높이 {row[4]:.0f}mm, "
              f"들어올림 {row[5]:.0f}mm, 수평이동 {row[6]:.0f}mm, {row[7]:.1f}초")
    print("\n[전체 평균 ± 표준편차]")
    for name, c in (("집기높이", 3), ("놓기높이", 4), ("들어올림", 5), ("수평이동", 6), ("길이(초)", 7)):
        print(f"  {name}: {v[:, c].mean():6.1f} ± {v[:, c].std():5.1f}   중앙 {np.median(v[:, c]):6.1f}")

    # ══════════ 그림 ══════════
    fig, axes = plt.subplots(2, 3, figsize=(19, 10.5))

    # 1) 시간 정규화 z 곡선 전체 + 평균
    ax = axes[0][0]
    u = np.linspace(0, 1, 100)
    for zc in Z:
        ax.plot(u, zc, color="0.75", lw=0.5, alpha=0.35)
    m, sd = Z.mean(0), Z.std(0)
    ax.fill_between(u, m - sd, m + sd, color="tab:blue", alpha=0.25, label="±1σ")
    ax.plot(u, m, color="tab:blue", lw=2.5, label="평균")
    ax.axvline(np.mean(tg_n), color="tab:red", ls="--", lw=1.5,
               label=f"평균 집기 시점 ({np.mean(tg_n)*100:.0f}%)")
    ax.axvline(np.mean(tr_n), color="tab:green", ls="--", lw=1.5,
               label=f"평균 놓기 시점 ({np.mean(tr_n)*100:.0f}%)")
    ax.axhline(0, color="k", lw=1)
    ax.set_xlabel("에피소드 진행률"); ax.set_ylabel("테이블 위 높이 [mm]")
    ax.set_title(f"전 에피소드 z 곡선 겹침 ({len(Z)}개) — 공통 구조 확인")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)

    # 2) 집기/놓기 위치 (TX90 XY)
    ax = axes[0][1]
    ax.scatter(*pick_xy.T, s=14, c="tab:red", alpha=0.7, label=f"집기 ({len(pick_xy)})")
    ax.scatter(*place_xy.T, s=14, c="tab:blue", alpha=0.7, label=f"놓기 ({len(place_xy)})")
    rx = [RECT_X[0], RECT_X[1], RECT_X[1], RECT_X[0], RECT_X[0]]
    ry = [RECT_Y[0], RECT_Y[0], RECT_Y[1], RECT_Y[1], RECT_Y[0]]
    ax.plot(rx, ry, "k--", lw=1.5, label="실측 사각형")
    ax.set_xlabel("x [mm]"); ax.set_ylabel("y [mm]")
    ax.set_title("집기·놓기 위치 (TX90 base_link)")
    ax.set_aspect("equal"); ax.legend(fontsize=9); ax.grid(alpha=0.3)

    # 3) 집기/놓기 높이 분포
    ax = axes[0][2]
    bins = np.arange(-5, 130, 5)
    ax.hist(v[:, 3], bins=bins, color="tab:red", alpha=0.65, label=f"집기 (평균 {v[:,3].mean():.0f}mm)")
    ax.hist(v[:, 4], bins=bins, color="tab:blue", alpha=0.55, label=f"놓기 (평균 {v[:,4].mean():.0f}mm)")
    ax.axvline(0, color="k", lw=1.5)
    ax.annotate("테이블면", (0, ax.get_ylim()[1] * 0.95), fontsize=9, ha="left")
    ax.set_xlabel("테이블 위 높이 [mm]"); ax.set_ylabel("에피소드 수")
    ax.set_title("집기·놓기 높이 분포 — 물체(~30mm)를 집고 내려놓는 높이")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)

    # 4) 들어올림 분포
    ax = axes[1][0]
    ax.hist(v[:, 5], bins=25, color="tab:purple", alpha=0.8)
    ax.axvline(v[:, 5].mean(), color="k", ls="--", lw=1.5,
               label=f"평균 {v[:,5].mean():.0f}mm")
    ax.set_xlabel("들어올림 [mm]"); ax.set_ylabel("에피소드 수")
    ax.set_title("집은 뒤 들어올린 높이")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)

    # 5) 수평이동 분포
    ax = axes[1][1]
    ax.hist(v[:, 6], bins=25, color="tab:green", alpha=0.8)
    ax.axvline(v[:, 6].mean(), color="k", ls="--", lw=1.5,
               label=f"평균 {v[:,6].mean():.0f}mm")
    ax.set_xlabel("집기→놓기 수평이동 [mm]"); ax.set_ylabel("에피소드 수")
    ax.set_title("운반 거리")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)

    # 6) 품질 지도: 수평이동 vs 놓기높이
    ax = axes[1][2]
    ax.scatter(r[clean, 6], r[clean, 4], s=18, c="0.55", label=f"깨끗 ({clean.sum()})")
    ax.scatter(r[retry, 6], r[retry, 4], s=30, c="tab:orange",
               label=f"재시도 ({retry.sum()})")
    ax.scatter(r[susp, 6], r[susp, 4], s=70, c="tab:red", marker="X",
               label=f"의심 ({susp.sum()})")
    for row in r[susp]:
        ax.annotate(f"ep{int(row[0])}", (row[6], row[4]),
                    textcoords="offset points", xytext=(8, 4),
                    color="tab:red", fontweight="bold", fontsize=10)
    ax.axhline(100, color="tab:red", ls=":", lw=1.2)
    ax.axvline(80, color="tab:red", ls=":", lw=1.2)
    ax.set_xlabel("수평이동 [mm]"); ax.set_ylabel("놓기 높이 [mm]")
    ax.set_title("품질 지도 — 정상은 오른쪽 아래, 의심은 왼쪽 위")
    ax.legend(fontsize=9); ax.grid(alpha=0.3)

    fig.suptitle(f"TX90 변환 데이터셋 전수 검사 — {valid.sum()}개 에피소드", fontsize=15, y=0.995)
    fig.tight_layout()
    fig.savefig(OUT, dpi=120)
    print(f"\n저장: {OUT}")


if __name__ == "__main__":
    main()
