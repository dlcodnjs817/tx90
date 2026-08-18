#!/usr/bin/env python3
"""omx_f.urdf 기반 FK로 LeRobot parquet의 joint 시퀀스에서 EE pose를 계산한다.

parquet의 observation.state / action 은 6차원:
    [joint1, joint2, joint3, joint4, joint5, gripper_joint_1]
EE 체인(world -> link0 -> ... -> link5 -> end_effector_link)에는
앞의 5개만 관여하고 gripper 는 영향이 없다.
"""

import argparse
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
from scipy.spatial.transform import Rotation as R

URDF = "omx_f.urdf"
TIP = "end_effector_link"
BASE = "world"


def load_chain(urdf_path, base, tip):
    """base -> tip 경로상의 joint 를 순서대로 반환한다."""
    root = ET.parse(urdf_path).getroot()

    joints = {}
    parent_of = {}
    for j in root.findall("joint"):
        name = j.get("name")
        child = j.find("child").get("link")
        origin = j.find("origin")
        xyz = np.zeros(3)
        rpy = np.zeros(3)
        if origin is not None:
            if origin.get("xyz"):
                xyz = np.array([float(v) for v in origin.get("xyz").split()])
            if origin.get("rpy"):
                rpy = np.array([float(v) for v in origin.get("rpy").split()])
        axis = np.array([1.0, 0.0, 0.0])
        a = j.find("axis")
        if a is not None and a.get("xyz"):
            axis = np.array([float(v) for v in a.get("xyz").split()])
        joints[name] = {
            "name": name,
            "type": j.get("type"),
            "parent": j.find("parent").get("link"),
            "child": child,
            "xyz": xyz,
            "rpy": rpy,
            "axis": axis,
        }
        parent_of[child] = name

    # tip 에서 base 까지 거슬러 올라간다
    chain = []
    link = tip
    while link != base:
        if link not in parent_of:
            raise RuntimeError(f"{link} 에서 {base} 까지 경로가 끊겼습니다")
        j = joints[parent_of[link]]
        chain.append(j)
        link = j["parent"]
    chain.reverse()
    return chain


def fk(chain, q):
    """q: chain 내 movable joint 의 각도(rad). 4x4 동차변환 반환."""
    T = np.eye(4)
    qi = 0
    for j in chain:
        Torigin = np.eye(4)
        Torigin[:3, :3] = R.from_euler("xyz", j["rpy"]).as_matrix()
        Torigin[:3, 3] = j["xyz"]

        Tjoint = np.eye(4)
        if j["type"] in ("revolute", "continuous"):
            axis = j["axis"] / np.linalg.norm(j["axis"])
            Tjoint[:3, :3] = R.from_rotvec(axis * q[qi]).as_matrix()
            qi += 1
        elif j["type"] == "prismatic":
            axis = j["axis"] / np.linalg.norm(j["axis"])
            Tjoint[:3, 3] = axis * q[qi]
            qi += 1

        T = T @ Torigin @ Tjoint
    return T


def poses_from_column(chain, col, n_movable):
    """joint 배열 시리즈 -> (N,3) 위치, (N,4) 쿼터니언(xyzw), (N,3) rpy"""
    q = np.stack([np.asarray(v, dtype=float)[:n_movable] for v in col])
    pos = np.empty((len(q), 3))
    quat = np.empty((len(q), 4))
    rpy = np.empty((len(q), 3))
    for i, qi in enumerate(q):
        T = fk(chain, qi)
        rot = R.from_matrix(T[:3, :3])
        pos[i] = T[:3, 3]
        quat[i] = rot.as_quat()          # x, y, z, w
        rpy[i] = rot.as_euler("xyz")     # roll, pitch, yaw
    return pos, quat, rpy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet", nargs="?", default="episode_000000.parquet")
    ap.add_argument("--urdf", default=URDF)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    chain = load_chain(args.urdf, BASE, TIP)
    movable = [j for j in chain if j["type"] not in ("fixed",)]
    n = len(movable)
    print(f"체인: {BASE} -> {TIP}")
    for j in chain:
        mark = "*" if j["type"] != "fixed" else " "
        print(f"  {mark} {j['name']:<20} {j['type']:<10} axis={j['axis']} xyz={j['xyz']}")
    print(f"가동 joint {n}개\n")

    df = pd.read_parquet(args.parquet)

    out = df[["timestamp", "frame_index", "episode_index"]].copy()
    for src, tag in (("observation.state", "state"), ("action", "action")):
        if src not in df.columns:
            continue
        pos, quat, rpy = poses_from_column(chain, df[src], n)
        out[f"ee_{tag}_x"], out[f"ee_{tag}_y"], out[f"ee_{tag}_z"] = pos.T
        out[f"ee_{tag}_roll"], out[f"ee_{tag}_pitch"], out[f"ee_{tag}_yaw"] = rpy.T
        (out[f"ee_{tag}_qx"], out[f"ee_{tag}_qy"],
         out[f"ee_{tag}_qz"], out[f"ee_{tag}_qw"]) = quat.T

    dst = args.out or args.parquet.replace(".parquet", "_ee_pose.csv")
    out.to_csv(dst, index=False)
    print(f"저장: {dst}  ({len(out)} 프레임)")

    p = out[["ee_state_x", "ee_state_y", "ee_state_z"]].to_numpy()
    print(f"\nEE 위치(state) 범위 [m]")
    print(f"  x: {p[:,0].min():+.4f} ~ {p[:,0].max():+.4f}")
    print(f"  y: {p[:,1].min():+.4f} ~ {p[:,1].max():+.4f}")
    print(f"  z: {p[:,2].min():+.4f} ~ {p[:,2].max():+.4f}")
    print(f"  이동 거리 합: {np.linalg.norm(np.diff(p, axis=0), axis=1).sum():.4f} m")
    print(f"\n처음 3 프레임:")
    print(out.head(3).to_string(index=False))


if __name__ == "__main__":
    main()
