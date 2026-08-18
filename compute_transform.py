#!/usr/bin/env python3
"""STEP 4 — Umeyama(scale 포함) 로 OMX → TX2-90 변환식 T(s, R, t) 계산 + 검증.

    pos_tx90 = s * (R @ pos_omx) + t

s 는 position 에만 쓴다. orientation 은 R 만 쓰고 s 를 곱하지 않는다 (STEP 5).

사용법
  1. 아래 ★ TX90 측정값 ★ 에 pendant 로 잰 4 코너를 채운다
  2. TX90_UNIT 을 pendant 단위에 맞춘다 ('mm' 또는 'm')
  3. python3 compute_transform.py
"""

import argparse
import os

import numpy as np

# ═════════════════════════ OMX 코너 (STEP 2 결과, 수정 불필요) ═════════════════════════
# extract_omx_workspace.py 출력. 단위 m. z 는 사각형 안 최저 도달 z = 테이블 표면.
OMX_CORNERS = np.array([
    [+0.111889, -0.159554, +0.028220],   # P1 (xmin, ymin)
    [+0.352661, -0.159554, +0.028220],   # P2 (xmax, ymin)
    [+0.352661, +0.201406, +0.028220],   # P3 (xmax, ymax)
    [+0.111889, +0.201406, +0.028220],   # P4 (xmin, ymax)
])
# 5 점으로 갈 경우 (높이를 아는 블록 윗면을 짚었을 때만 의미가 있다)
# OMX_CORNERS = np.vstack([OMX_CORNERS, [+0.111889, -0.159554, +0.239371]])   # P5

# ═════════════════════════ ★ TX90 측정값 — 여기를 채우세요 ★ ═════════════════════════
# pendant: World/Frame 모드, TCP = tool 없음(flange 기준).
# OMX 의 P1~P4 와 같은 순서로. 테이블에 241 x 361 mm (가로:세로 1:1.499) 사각형.
TX90_UNIT = "mm"          # pendant 표시 단위: 'mm' 또는 'm'

# 펜던트가 표시하는 World 원점은 URDF 의 base_link 가 아니라 ROS-Industrial 'base' 링크다.
# URDF: <joint base_link-base> origin xyz="0 0 0.478"  →  base = base_link + 478mm
# 측정한 5개 자세의 joint 값을 URDF 규약대로 FK 해서 확인했다.
# X, Y 는 0.1mm 이내로 일치하고 Z 만 5개 전부 정확히 478.0mm 차이났다.
# MoveIt 요청은 base_link 프레임으로 보내므로 여기서 z 에 더해 변환한다.
PENDANT_Z_OFFSET = 0.478  # m, base → base_link

TX90_CORNERS = np.array([
    [611.84, -173.72, -143.89],   # P1
    [852.56, -173.72, -143.89],   # P2
    [852.53, +187.17, -143.97],   # P3
    [611.75, +187.17, -143.97],   # P4
])
# 5 점으로 갈 경우 아래 줄 주석 해제 후 추가
# TX90_CORNERS = np.vstack([TX90_CORNERS, [np.nan, np.nan, np.nan]])          # P5
# ═══════════════════════════════════════════════════════════════════════════════════


def umeyama(src, dst, with_scale=True):
    """Umeyama(1991). src, dst: (N,3). 반환 (s, R, t) — dst ≈ s*(R@src) + t."""
    n = len(src)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    Xs, Xd = src - mu_s, dst - mu_d

    C = Xd.T @ Xs / n
    U, D, Vt = np.linalg.svd(C)

    # 반사(reflection) 방지
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[-1, -1] = -1.0

    R = U @ S @ Vt
    var_s = (Xs ** 2).sum() / n
    s = float((D * np.diag(S)).sum() / var_s) if with_scale else 1.0
    t = mu_d - s * (R @ mu_s)
    return s, R, t


def apply_T(s, R, t, pts):
    return (s * (R @ np.atleast_2d(pts).T).T) + t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/omx_workspace"))
    ap.add_argument("--no_scale", action="store_true",
                    help="Kabsch (s=1 고정) 로 계산")
    args = ap.parse_args()

    src = np.asarray(OMX_CORNERS, dtype=float)
    dst = np.asarray(TX90_CORNERS, dtype=float)

    if np.isnan(dst).any():
        raise SystemExit(
            "TX90_CORNERS 가 아직 비어 있습니다.\n"
            "  이 파일 상단 ★ TX90 측정값 ★ 에 pendant 로 잰 4 코너를 채우고,\n"
            "  TX90_UNIT 을 pendant 단위('mm' 또는 'm')에 맞춘 뒤 다시 실행하세요.")
    if len(src) != len(dst):
        raise SystemExit(f"점 개수 불일치: OMX {len(src)}개 vs TX90 {len(dst)}개")
    if len(src) < 3:
        raise SystemExit("최소 3 점이 필요합니다")

    # ── 단위 변환 ──
    if TX90_UNIT == "mm":
        dst = dst / 1000.0
        print("TX90 측정값 mm → m 변환 적용")
    elif TX90_UNIT == "m":
        print("TX90 측정값 단위 m — 변환 없음")
    else:
        raise SystemExit(f"TX90_UNIT 은 'mm' 또는 'm' 이어야 합니다 (현재: {TX90_UNIT})")

    if PENDANT_Z_OFFSET:
        dst = dst + np.array([0.0, 0.0, PENDANT_Z_OFFSET])
        print(f"펜던트 base 프레임 → base_link 변환: z 에 {PENDANT_Z_OFFSET*1000:+.1f} mm")

    # ── 측정값 사전 점검 ──
    print(f"\n[대응점] {len(src)}개")
    for i, (a, b) in enumerate(zip(src, dst), 1):
        print(f"  P{i}  OMX [{a[0]:+.4f} {a[1]:+.4f} {a[2]:+.4f}]"
              f"   TX90 [{b[0]:+.4f} {b[1]:+.4f} {b[2]:+.4f}]")

    # 테이블 기울기 점검 — 4 코너 z 가 일치해야 한다
    zs = dst[:4, 2]
    if np.ptp(zs) > 0.005:
        print(f"\n  [경고] TX90 4 코너의 z 가 {np.ptp(zs)*1000:.1f} mm 어긋납니다 "
              f"({zs.min()*1000:.1f} ~ {zs.max()*1000:.1f} mm).")
        print("         테이블이 로봇 베이스 기준으로 기울었거나 측정 오차입니다. "
              "5 mm 넘으면 재측정 권장.")
    else:
        print(f"\n  4 코너 z 편차 {np.ptp(zs)*1000:.1f} mm — 테이블 수평 양호")

    # 변끼리 길이 비교 — 사각형이 찌그러졌는지
    def edges(p):
        return np.array([np.linalg.norm(p[(i + 1) % 4] - p[i]) for i in range(4)])
    eo, et = edges(src[:4]), edges(dst[:4])
    print(f"\n  변 길이 [mm]  OMX  {np.round(eo*1000, 1)}")
    print(f"                TX90 {np.round(et*1000, 1)}")
    print(f"  변별 비율     {np.round(et/eo, 4)}   "
          f"(편차 {np.ptp(et/eo):.4f} — 0 에 가까울수록 닮은꼴)")

    # ── Umeyama ──
    s, R, t = umeyama(src, dst, with_scale=not args.no_scale)
    pred = apply_T(s, R, t, src)
    err = np.linalg.norm(pred - dst, axis=1)

    print(f"\n[변환식]")
    print(f"  s = {s:.6f}")
    print(f"  R =\n{np.array2string(R, precision=6, prefix='      ')}")
    print(f"  t = [{t[0]:+.6f}, {t[1]:+.6f}, {t[2]:+.6f}] m")
    rz = np.degrees(np.arctan2(R[1, 0], R[0, 0]))
    print(f"  (Z축 기준 회전 약 {rz:+.2f}°)")

    print(f"\n[residual]")
    for i, e in enumerate(err, 1):
        print(f"  P{i}: {e*1000:6.2f} mm")
    print(f"  mean = {err.mean()*1000:.2f} mm,  max = {err.max()*1000:.2f} mm")

    ax_err = np.abs(pred - dst).mean(0) * 1000
    print(f"  축별 평균오차 [mm]  x {ax_err[0]:.2f}  y {ax_err[1]:.2f}  z {ax_err[2]:.2f}")

    # ── 판정 ──
    m = err.mean() * 1000
    print(f"\n[판정]")
    if m < 5:
        print(f"  mean {m:.2f} mm < 5 mm — 성공. STEP 5 로 진행하세요.")
    elif m < 20:
        print(f"  mean {m:.2f} mm — 측정 정밀도 문제. 코너 재측정 권장.")
    else:
        print(f"  mean {m:.2f} mm — 단위 착오(mm↔m) 또는 코너 순서(P1~P4) 오류를 먼저 점검하세요.")
    if m >= 10 and ax_err.max() > 2 * np.median(ax_err):
        print(f"  특정 축({'xyz'[int(np.argmax(ax_err))]})에만 오차가 큽니다. "
              f"진짜 비등방(anisotropic)일 수 있으니 축별 affine 확장을 검토하세요.")

    if len(src) == 4 and np.ptp(src[:, 2]) < 1e-6:
        print("\n  참고: 4 점이 모두 같은 평면이라 residual 은 두 사각형이 닮은꼴인지와\n"
              "        단위·코너순서가 맞는지만 봅니다. 전이가 맞는지의 검증은 STEP 6(RViz) 입니다.")
    if not args.no_scale and not (0.8 < s < 1.25):
        print(f"\n  [경고] s = {s:.4f} 가 1 에서 많이 벗어났습니다. 두 사각형을 같은 크기로\n"
              f"         잡았다면 s 는 1 근처여야 합니다. 단위 착오를 의심하세요.")

    # ── 저장 ──
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "transform.npy")
    np.save(path, {"s": s, "R": R, "t": t,
                   "residual_mean_mm": float(err.mean()),
                   "residual_max_mm": float(err.max()),
                   "n_points": len(src)}, allow_pickle=True)
    print(f"\n저장: {path}")
    print("  불러오기: d = np.load(path, allow_pickle=True).item(); "
          "s, R, t = d['s'], d['R'], d['t']")


if __name__ == "__main__":
    main()
