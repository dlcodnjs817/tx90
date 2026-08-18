#!/usr/bin/env python3
"""STEP 1 — EE 데이터셋 구조 확인.

163개 parquet 전체를 훑어서 컬럼 구성·배열 길이·값 범위가 일관되는지 확인한다.
meta/info.json 은 낡아서 믿지 않고 parquet 실값만 본다.
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

LABELS = ["x", "y", "z", "rx", "ry", "rz", "gripper"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", default=os.path.expanduser(
        "~/omx_act_pick_and_place_v4_162_ee"))
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.data_dir, "**", "*.parquet"),
                             recursive=True))
    print(f"parquet 파일: {len(files)}개\n")

    # --- meta/info.json 과 실제 데이터 비교 ---
    info_path = os.path.join(args.data_dir, "meta", "info.json")
    if os.path.exists(info_path):
        info = json.load(open(info_path))
        feat = info.get("features", {})
        st = feat.get("observation.state", {})
        print(f"[info.json] observation.state shape={st.get('shape')} "
              f"names={st.get('names')}")

    # --- 전체 스캔 ---
    col_sets, lengths, rows, eps = set(), set(), [], []
    lo = np.full(16, np.inf)
    hi = np.full(16, -np.inf)
    ndim = None
    mismatch = []

    for f in files:
        df = pd.read_parquet(f)
        col_sets.add(tuple(df.columns))
        rows.append(len(df))
        eps.extend(df["episode_index"].unique().tolist())

        for c in ("observation.state", "action"):
            arr = np.stack(df[c].to_numpy())
            lengths.add(arr.shape[1])
            if ndim is None:
                ndim = arr.shape[1]
            if c == "observation.state":
                lo[:ndim] = np.minimum(lo[:ndim], arr.min(axis=0))
                hi[:ndim] = np.maximum(hi[:ndim], arr.max(axis=0))
            if not np.isfinite(arr).all():
                mismatch.append(f"{os.path.basename(f)} {c}: NaN/Inf 있음")

    print(f"\n[컬럼 구성] 고유 조합 {len(col_sets)}개")
    for cs in col_sets:
        print(f"  {list(cs)}")
    print(f"\n[배열 길이] 고유 값 {sorted(lengths)}")
    print(f"[프레임 수] 합계 {sum(rows)}, 최소 {min(rows)}, 최대 {max(rows)}")

    eps = sorted(set(eps))
    print(f"[episode_index] {len(eps)}개, 범위 {min(eps)}~{max(eps)}")
    missing = sorted(set(range(min(eps), max(eps) + 1)) - set(eps))
    print(f"  결번: {missing if missing else '없음'}")

    print(f"\n[observation.state 전체 min/max]")
    for i in range(ndim):
        label = LABELS[i] if i < len(LABELS) else f"idx{i}"
        print(f"  idx{i} ({label:>7s}): {lo[i]:+9.4f} ~ {hi[i]:+9.4f}")

    if mismatch:
        print("\n[경고]")
        for m in mismatch:
            print("  " + m)
    else:
        print("\nNaN/Inf 없음")


if __name__ == "__main__":
    main()
