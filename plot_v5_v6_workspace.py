#!/usr/bin/env python3
"""데이터셋 v5(TX90 EE pose) / v6(TX90 joint) 전수 분석 그림.

omx_workspace.png(OMX 원본 분석)와 같은 4패널 스타일로, 변환 단계별 데이터를
시각화한다:
  tx90_v5_workspace.png — Umeyama 변환 후 TX90 base_link EE pose (학습 v5 그대로)
  tx90_v6_workspace.png — 오프셋(−0.20,+0.15) + IK 변환 후 관절 궤적 (학습 v6 그대로)

  python3 plot_v5_v6_workspace.py
"""

import glob

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

BASE = "/home/kim/physical_ai_tools/docker/huggingface/lerobot/dlcodnjs"
V5 = f"{BASE}/tx90_act_pick_and_place_v5_ee/data/chunk-000"
V6 = f"{BASE}/tx90_act_pick_and_place_v6_joint/data/chunk-000"
OUT = "/home/kim/tx90/omx_workspace"
GRIP_TH = 0.459
OFFSET = np.array([-0.20, 0.15, 0.0])
JOINT_LIMITS_DEG = [(-180, 180), (-130, 147.5), (-145, 145),
                    (-270, 270), (-115, 140), (-270, 270)]

# OMX 코너 → TX90 (Umeyama)
tf = np.load("/home/kim/tx90/omx_workspace/transform.npy", allow_pickle=True).item()
OMX_CORNERS = np.array([
    [+0.111889, -0.159554, +0.028220],
    [+0.352661, -0.159554, +0.028220],
    [+0.352661, +0.201406, +0.028220],
    [+0.111889, +0.201406, +0.028220],
])
CORN_TX = (tf["s"] * (tf["R"] @ OMX_CORNERS.T)).T + tf["t"]


def load(dirpath):
    """에피소드별 action 배열과 grasp/release 인덱스."""
    eps = []
    for p in sorted(glob.glob(f"{dirpath}/episode_*.parquet")):
        a = np.stack(pd.read_parquet(p)["action"]).astype(float)
        g = a[:, 6] < GRIP_TH
        grasp = release = None
        if g.any():
            grasp = int(np.argmax(g))
            after = np.where(~g[grasp:])[0]
            if len(after):
                release = grasp + int(after[0])
        eps.append((a, grasp, release))
    return eps


def spatial_panels(ax1, ax2, ax3, pos_all, grasp_pts, rel_pts, corners, table_z):
    sub = pos_all[:: max(1, len(pos_all) // 20000)]
    ax1.scatter(sub[:, 0], sub[:, 1], s=1, c="0.55", alpha=0.15, label="전체 궤적")
    ax1.scatter(grasp_pts[:, 0], grasp_pts[:, 1], s=18, c="tab:red",
                label=f"grasp ({len(grasp_pts)})")
    ax1.scatter(rel_pts[:, 0], rel_pts[:, 1], s=18, c="tab:blue",
                label=f"release ({len(rel_pts)})")
    rect = np.vstack([corners, corners[:1]])
    ax1.plot(rect[:, 0], rect[:, 1], "r-", lw=2)
    for i, (x, y, _z) in enumerate(corners):
        ax1.annotate(f"P{i+1}", (x, y), textcoords="offset points",
                     xytext=(8, 8), color="red", fontweight="bold")
    w = np.linalg.norm(corners[1] - corners[0]) * 1000
    h = np.linalg.norm(corners[2] - corners[1]) * 1000
    ax1.set_title(f"XY (top view)   W x H = {w:.0f} x {h:.0f} mm")
    ax1.set_xlabel("x [m]"); ax1.set_ylabel("y [m]")
    ax1.set_aspect("equal"); ax1.grid(alpha=0.3); ax1.legend(fontsize=8)

    ax2.scatter(sub[:, 0], sub[:, 2], s=1, c="0.55", alpha=0.15)
    ax2.scatter(grasp_pts[:, 0], grasp_pts[:, 2], s=18, c="tab:red")
    ax2.scatter(rel_pts[:, 0], rel_pts[:, 2], s=18, c="tab:blue")
    ax2.axhline(table_z, color="red", ls="--", lw=1.5,
                label=f"기준면 z = {table_z*1000:.0f} mm")
    ax2.set_title("XZ (side view)")
    ax2.set_xlabel("x [m]"); ax2.set_ylabel("z [m]")
    ax2.grid(alpha=0.3); ax2.legend(fontsize=8)

    ax3.hist((pos_all[:, 2] - table_z) * 1000, bins=120, color="0.6")
    gz = (np.concatenate([grasp_pts[:, 2], rel_pts[:, 2]]) - table_z) * 1000
    ax3.hist(gz, bins=30, color="tab:red", alpha=0.8, label="grasp/release 높이")
    ax3.axvline(0, color="red", ls="--", lw=1.5)
    ax3.set_title("기준면 대비 z 분포")
    ax3.set_xlabel("z - 기준면 [mm]"); ax3.set_ylabel("frames")
    ax3.grid(alpha=0.3); ax3.legend(fontsize=8)


def grip_panel(ax, grips):
    ax.hist(grips, bins=100, color="tab:green")
    ax.axvline(GRIP_TH, color="red", ls="--", label=f"임계값 {GRIP_TH}")
    ax.set_title("그리퍼 값 분포 (개폐 판정)")
    ax.set_xlabel("gripper [rad]"); ax.set_ylabel("frames")
    ax.grid(alpha=0.3); ax.legend(fontsize=8)


# ══════════════ v5 ══════════════
eps5 = load(V5)
pos_all = np.vstack([a[:, :3] for a, _g, _r in eps5])
grasp_pts = np.array([a[g, :3] for a, g, r in eps5 if g is not None])
rel_pts = np.array([a[r, :3] for a, g, r in eps5 if r is not None])
grips5 = np.concatenate([a[:, 6] for a, _g, _r in eps5])
table_z5 = float(CORN_TX[:, 2].mean())

fig, axes = plt.subplots(1, 4, figsize=(27.3, 6.0))
fig.suptitle("데이터셋 v5 — TX90 base_link EE pose (OMX→Umeyama 변환 후, 학습 입력 그대로 · IK 이전)",
             fontsize=14, y=1.00)
spatial_panels(axes[0], axes[1], axes[2], pos_all, grasp_pts, rel_pts,
               CORN_TX, table_z5)
grip_panel(axes[3], grips5)
fig.tight_layout()
fig.savefig(f"{OUT}/tx90_v5_workspace.png", dpi=100, bbox_inches="tight")
print(f"저장: {OUT}/tx90_v5_workspace.png")

# ══════════════ v6 ══════════════
eps6 = load(V6)
J_all = np.vstack([a[:, :6] for a, _g, _r in eps6])
steps = np.concatenate([np.degrees(np.abs(np.diff(a[:, :6], axis=0))).max(axis=1)
                        for a, _g, _r in eps6])
grips6 = np.concatenate([a[:, 6] for a, _g, _r in eps6])

fig, axes = plt.subplots(1, 4, figsize=(27.3, 6.0))
fig.suptitle("데이터셋 v6 — TX90 joint (v5 + 작업대 오프셋(−0.20,+0.15) + 오프라인 IK 변환, 학습 라벨 그대로)",
             fontsize=14, y=1.00)

# ① EE 실행 위치 (= v5 위치 + 오프셋, FK 역검증 오차 ≤5.1mm)
ax = axes[0]
sub = pos_all[:: max(1, len(pos_all) // 20000)] + OFFSET
ax.scatter(sub[:, 0], sub[:, 1], s=1, c="0.55", alpha=0.15, label="전체 궤적")
gp = grasp_pts + OFFSET
rp_ = rel_pts + OFFSET
ax.scatter(gp[:, 0], gp[:, 1], s=18, c="tab:red", label=f"grasp ({len(gp)})")
ax.scatter(rp_[:, 0], rp_[:, 1], s=18, c="tab:blue", label=f"release ({len(rp_)})")
corn6 = CORN_TX + OFFSET
rect = np.vstack([corn6, corn6[:1]])
ax.plot(rect[:, 0], rect[:, 1], "r-", lw=2)
for i, (x, y, _z) in enumerate(corn6):
    ax.annotate(f"P{i+1}", (x, y), textcoords="offset points",
                xytext=(8, 8), color="red", fontweight="bold")
ax.set_title("실행 위치 XY — 작업대 오프셋 반영 (FK 검증 오차 ≤5.1mm)")
ax.set_xlabel("x [m]"); ax.set_ylabel("y [m]")
ax.set_aspect("equal"); ax.grid(alpha=0.3); ax.legend(fontsize=8)

# ② 관절별 값 범위
ax = axes[1]
parts = ax.violinplot([np.degrees(J_all[:, k]) for k in range(6)],
                      showextrema=True, widths=0.8)
for i, (lo, hi) in enumerate(JOINT_LIMITS_DEG):
    ax.plot([i + 0.7, i + 1.3], [lo, lo], "r--", lw=1)
    ax.plot([i + 0.7, i + 1.3], [hi, hi], "r--", lw=1)
ax.set_xticks(range(1, 7), [f"j{k}" for k in range(1, 7)])
ax.set_title("관절별 값 분포 (빨간 점선 = 관절 한계)")
ax.set_ylabel("각도 [deg]"); ax.grid(alpha=0.3)

# ③ 프레임 간 관절 이동 (연속성)
ax = axes[2]
ax.hist(steps, bins=120, color="0.6", log=True)
ax.axvline(16.22, color="tab:orange", ls="--", label="실측 최대 16.2°")
ax.axvline(30, color="red", ls="--", label="안전 필터 임계 30°")
ax.set_title("프레임 간 최대 관절 이동 (30Hz, 연속성 검증)")
ax.set_xlabel("max |Δjoint| [deg/frame]"); ax.set_ylabel("frames (log)")
ax.grid(alpha=0.3); ax.legend(fontsize=8)

grip_panel(axes[3], grips6)
fig.tight_layout()
fig.savefig(f"{OUT}/tx90_v6_workspace.png", dpi=100, bbox_inches="tight")
print(f"저장: {OUT}/tx90_v6_workspace.png")
