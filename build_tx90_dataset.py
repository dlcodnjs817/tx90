#!/usr/bin/env python3
"""OMX LeRobot 데이터셋 → TX2-90 EE pose 학습용 데이터셋 조립.

컨테이너(physical_ai_server) 안에서 실행한다. lerobot 과 원본 데이터셋이 거기 있다.

하는 일
  1. observation.state / action 을 TX90 EE pose 로 변환  [x,y,z,rx,ry,rz,gripper]
  2. 카메라 영상은 원본을 심볼릭 링크 (내용이 안 바뀌므로 복사할 이유가 없다)
  3. 불량 에피소드를 빼고 나머지를 0..N-1 로 재번호
     — lerobot 은 episode_index 가 빈틈없이 이어진다고 가정한다
  4. episodes_stats 재계산 — 값이 joint(rad) 에서 EE(m) 로 바뀌어 정규화 통계를
     그대로 쓰면 학습이 망가진다. 영상 통계는 안 바뀌므로 그대로 옮긴다.

  python3 build_tx90_dataset.py --drop 62
"""

import argparse
import json
import os
import shutil

import numpy as np
import pandas as pd

# 컨테이너에 scipy 가 없어서 numpy 로 직접 구현한다.
# 규약은 extrinsic xyz (scipy 의 Rotation.from_euler("xyz") 와 동일).

R_TOOL = np.array([[0.0, 0.0, 1.0],
                   [0.0, 1.0, 0.0],
                   [-1.0, 0.0, 0.0]])          # Ry(+90도)


def euler_to_mat(e):
    """(N,3) extrinsic xyz 오일러 → (N,3,3). R = Rz @ Ry @ Rx."""
    cx, cy, cz = np.cos(e).T
    sx, sy, sz = np.sin(e).T
    M = np.empty((len(e), 3, 3))
    M[:, 0, 0] = cy * cz
    M[:, 0, 1] = sx * sy * cz - cx * sz
    M[:, 0, 2] = cx * sy * cz + sx * sz
    M[:, 1, 0] = cy * sz
    M[:, 1, 1] = sx * sy * sz + cx * cz
    M[:, 1, 2] = cx * sy * sz - sx * cz
    M[:, 2, 0] = -sy
    M[:, 2, 1] = sx * cy
    M[:, 2, 2] = cx * cy
    return M


def mat_to_euler(M):
    """(N,3,3) → (N,3) extrinsic xyz 오일러. 짐벌락(|sy|~1) 도 처리한다."""
    sy = np.clip(-M[:, 2, 0], -1.0, 1.0)
    ry = np.arcsin(sy)
    rx = np.arctan2(M[:, 2, 1], M[:, 2, 2])
    rz = np.arctan2(M[:, 1, 0], M[:, 0, 0])
    lock = np.abs(sy) > 1.0 - 1e-9          # cy ~ 0 이면 rx 와 rz 가 겹친다
    if lock.any():
        rx[lock] = 0.0
        rz[lock] = np.arctan2(-M[lock, 0, 1], M[lock, 1, 1])
    return np.stack([rx, ry, rz], axis=1)
EE_NAMES = ["x", "y", "z", "rx", "ry", "rz", "gripper"]
COLS = ("observation.state", "action")
CHUNK = "chunk-000"


def transform_rows(arr, s, R, t):
    out = np.empty((len(arr), 7), dtype=float)
    out[:, :3] = (s * (R @ arr[:, :3].T).T) + t
    Rm = euler_to_mat(arr[:, 3:6])
    out[:, 3:6] = mat_to_euler(R @ Rm @ R_TOOL)
    out[:, 6] = arr[:, 6]
    return out


def stats_of(a):
    """lerobot episodes_stats 형식. (N,D) 또는 (N,) 모두 처리."""
    a = np.asarray(a, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    return {"min": a.min(0).tolist(), "max": a.max(0).tolist(),
            "mean": a.mean(0).tolist(), "std": a.std(0).tolist(),
            "count": [len(a)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/root/.cache/huggingface/lerobot/dlcodnjs/"
                                     "omx_act_pick_and_place_v4_162")
    ap.add_argument("--ee_src", default="/root/omx_act_pick_and_place_v4_162_ee",
                    help="EE pose(7차원) 발췌본. state/action 의 입력이 된다")
    ap.add_argument("--transform", default="/root/transform.npy")
    ap.add_argument("--out", default="/root/.cache/huggingface/lerobot/dlcodnjs/"
                                     "tx90_act_pick_and_place_v4_162_ee")
    ap.add_argument("--drop", type=int, nargs="*", default=[62],
                    help="제외할 원본 episode_index")
    ap.add_argument("--copy_videos", action="store_true",
                    help="심볼릭 링크 대신 복사")
    args = ap.parse_args()

    d = np.load(args.transform, allow_pickle=True).item()
    s, R, t = float(d["s"]), np.asarray(d["R"]), np.asarray(d["t"])
    print(f"[transform] s={s:.6f}  t=[{t[0]:+.4f}, {t[1]:+.4f}, {t[2]:+.4f}] m")

    info = json.load(open(os.path.join(args.src, "meta", "info.json")))
    eps_meta = {json.loads(l)["episode_index"]: json.loads(l)
                for l in open(os.path.join(args.src, "meta", "episodes.jsonl"))}
    stats_meta = {json.loads(l)["episode_index"]: json.loads(l)
                  for l in open(os.path.join(args.src, "meta", "episodes_stats.jsonl"))}
    video_keys = [k for k in info["features"] if k.startswith("observation.images")]
    print(f"[원본] 에피소드 {len(eps_meta)}개, 카메라 {video_keys}")
    print(f"[제외] {args.drop}\n")

    out_data = os.path.join(args.out, "data", CHUNK)
    out_meta = os.path.join(args.out, "meta")
    os.makedirs(out_data, exist_ok=True)
    os.makedirs(out_meta, exist_ok=True)
    for k in video_keys:
        os.makedirs(os.path.join(args.out, "videos", CHUNK, k), exist_ok=True)

    old_eps = sorted(e for e in eps_meta if e not in args.drop)
    new_episodes, new_stats = [], []
    gidx = 0                                   # 전역 프레임 번호 재부여

    for new_ep, old_ep in enumerate(old_eps):
        src_pq = os.path.join(args.ee_src, "data", CHUNK, f"episode_{old_ep:06d}.parquet")
        df = pd.read_parquet(src_pq)

        for c in COLS:
            df[c] = list(transform_rows(
                np.stack(df[c].to_numpy()).astype(float), s, R, t))

        n = len(df)
        df["episode_index"] = new_ep
        df["index"] = np.arange(gidx, gidx + n)
        gidx += n
        df.to_parquet(os.path.join(out_data, f"episode_{new_ep:06d}.parquet"), index=False)

        # 영상 — 이름을 새 번호로 바꿔서 링크
        for k in video_keys:
            src_v = os.path.join(args.src, "videos", CHUNK, k, f"episode_{old_ep:06d}.mp4")
            dst_v = os.path.join(args.out, "videos", CHUNK, k, f"episode_{new_ep:06d}.mp4")
            if os.path.lexists(dst_v):
                os.remove(dst_v)
            if args.copy_videos:
                shutil.copy2(src_v, dst_v)
            else:
                os.symlink(os.path.relpath(src_v, os.path.dirname(dst_v)), dst_v)

        em = dict(eps_meta[old_ep]); em["episode_index"] = new_ep
        new_episodes.append(em)

        # 통계 — 값이 바뀐 것만 다시 계산하고 영상 통계는 그대로 옮긴다
        st = dict(stats_meta[old_ep]["stats"])
        for c in COLS:
            st[c] = stats_of(np.stack(df[c].to_numpy()))
        for c in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
            if c in st:
                st[c] = stats_of(df[c].to_numpy())
        new_stats.append({"episode_index": new_ep, "stats": st})

        if new_ep % 40 == 0 or new_ep == len(old_eps) - 1:
            print(f"  {new_ep+1}/{len(old_eps)}  (원본 ep{old_ep} → ep{new_ep}, {n} 프레임)")

    # ── meta ──
    with open(os.path.join(out_meta, "episodes.jsonl"), "w") as f:
        for e in new_episodes:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(os.path.join(out_meta, "episodes_stats.jsonl"), "w") as f:
        for e in new_stats:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    shutil.copy(os.path.join(args.src, "meta", "tasks.jsonl"),
                os.path.join(out_meta, "tasks.jsonl"))

    for c in COLS:
        info["features"][c] = {"dtype": "float32", "shape": [7], "names": EE_NAMES}
    info["total_episodes"] = len(old_eps)
    info["total_frames"] = gidx
    info["splits"] = {"train": f"0:{len(old_eps)}"}
    info["tx90_transform"] = {
        "s": s, "R": R.tolist(), "t": t.tolist(),
        "frame": "base_link", "ee_link": "tool0",
        "tool_fix": "Ry(+90deg)  OMX end_effector_link(+X) -> TX90 tool0(+Z)",
        "source_dataset": args.src,
        "dropped_episodes": args.drop,
        "residual_mean_mm": float(d.get("residual_mean_mm", np.nan)) * 1000,
    }
    with open(os.path.join(out_meta, "info.json"), "w") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    shutil.copy(args.transform, os.path.join(out_meta, "transform.npy"))

    print(f"\n[출력] {args.out}")
    print(f"  에피소드 {len(old_eps)}개 (원본 {len(eps_meta)} − 제외 {len(args.drop)}), "
          f"총 {gidx:,} 프레임")
    print(f"  영상: {'복사' if args.copy_videos else '심볼릭 링크'} "
          f"{len(old_eps)*len(video_keys)}개")
    print(f"  features: observation.state / action = 7차원 {EE_NAMES}")


if __name__ == "__main__":
    main()
