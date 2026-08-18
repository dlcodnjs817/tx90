#!/usr/bin/env python3
"""run_policy_tx90.py  —  STEP 8: 학습된 ACT 정책 → MoveIt2 → RViz 배선

정책이 뱉는 action 7개 [x, y, z, rx, ry, rz, gripper] 는 **이미 TX90 base_link
프레임 + tool0 규약**이다. 학습 데이터셋(build_tx90_dataset.py)을 만들 때
Umeyama 변환과 tool frame 90° 보정(Ry+90°)을 미리 적용했기 때문이다.
따라서 여기서는 변환을 **하지 않는다** — 또 하면 이중 변환으로 궤적이 틀어진다.
오일러(extrinsic xyz) → 쿼터니언 변환과 MoveIt2 연결만 한다.

두 모드
  predict  정책 추론 (컨테이너 안, torch 필요, ROS 불필요)
           데이터셋 에피소드의 관측(영상 2대 + EE pose)을 프레임 순서대로 넣어
           예측 궤적을 만들고, 정답(action)과 비교 통계를 찍는다.
           ※ open-loop: 관측은 항상 데이터셋의 실측을 쓴다. 실기 전 검증용.
  replay   RViz 재생 (컨테이너 안, ROS 필요, demo.launch.py 먼저 실행)
           예측 궤적(또는 데이터셋 GT)을 MoveIt2 Cartesian 으로 계획해 재생한다.
           dedup → 시간 균등 배분 → 50Hz 선형보간 → fake_controller 토픽 발행,
           어제 잡은 4가지 RViz 함정을 전부 반영했다.

사용 예 (컨테이너 안)
  # 1) 체크포인트로 에피소드 0 추론 → 예측 parquet 저장
  python3 run_policy_tx90.py predict \
      --checkpoint /root/train_tx90_act/checkpoints/last/pretrained_model \
      --episode 0 --out /root/policy_rollouts/ep000_pred.parquet
  # 2) 예측 궤적을 RViz 에서 재생
  python3 run_policy_tx90.py replay --parquet /root/policy_rollouts/ep000_pred.parquet
  # (비교용) 같은 에피소드의 정답 궤적 재생 — 데이터셋 parquet 을 직접 지정
  python3 run_policy_tx90.py replay --parquet \
      /root/.cache/huggingface/lerobot/dlcodnjs/tx90_act_pick_and_place_v4_162_ee/data/chunk-000/episode_000000.parquet
"""

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

# ── 선택 import: predict 는 torch/lerobot, replay 는 rclpy 가 필요하다.
#    없는 쪽 기능만 막고, --dry_run 은 어느 쪽 없이도 돌게 한다 (v2 와 같은 방식).
try:
    import rclpy
    from rclpy.node import Node
    from geometry_msgs.msg import Pose, PoseArray, PoseStamped
    from moveit_msgs.msg import RobotState, RobotTrajectory, DisplayTrajectory
    from moveit_msgs.srv import GetCartesianPath, GetPositionIK
    from sensor_msgs.msg import JointState
    from std_msgs.msg import Header
    from trajectory_msgs.msg import JointTrajectoryPoint
    ROS_OK, ROS_MISSING = True, None
except ModuleNotFoundError as _e:
    ROS_OK, ROS_MISSING = False, _e.name

    class Node:
        pass

    RobotTrajectory = object

# ═════════════════════════════════ 설정 ═════════════════════════════════
JOINT_NAMES = ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6"]

# 2026-08-12 pendant 실측. 규약은 URDF 와 동일(deg→rad 만 하면 된다).
HOME_UNIT = "deg"
HOME_JOINTS = [-3.31, 56.92, 73.58, -1.04, 48.85, -47.38]

EE_LINK = "tool0"
GROUP_NAME = "tx2_90_arm"
BASE_FRAME = "base_link"

# demo.launch.py 의 joint_state_publisher 가 이 토픽을 중계한다.
# /joint_states 에 직접 쏘면 이중 발행으로 all-zero 튐이 재현된다.
JOINT_TOPIC = "/move_group/fake_controller_joint_states"

SHOULDER = np.array([0.0, 0.0, 0.478])   # base_link 기준 어깨 위치
MAX_REACH = 0.95                          # m
GRIP_TH = 0.459                           # 닫힘~0.23 / 열림~0.69 의 중간

# tx2_90.urdf 의 joint 한계 (rad). IK 해의 2pi 접기에 쓴다.
JOINT_LIMITS = [(-3.1416, 3.1416), (-2.2689, 2.5744), (-2.5307, 2.5307),
                (-4.7124, 4.7124), (-2.0071, 2.4435), (-4.7124, 4.7124)]

DEFAULT_REPO = "dlcodnjs/tx90_act_pick_and_place_v5_ee"   # v5 = 오일러 wrap 수정본
# ════════════════════════════════════════════════════════════════════════


# ─────────────────────────── 회전 유틸 (v2 와 동일) ───────────────────────────
def euler_xyz_to_matrix(rx, ry, rz):
    """extrinsic xyz 오일러 → 회전행렬. R = Rz @ Ry @ Rx."""
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def matrix_to_quaternion(M):
    """회전행렬 → 쿼터니언 [x, y, z, w] (Shepperd)."""
    m00, m01, m02 = M[0]
    m10, m11, m12 = M[1]
    m20, m21, m22 = M[2]
    tr = m00 + m11 + m22
    if tr > 0.0:
        S = np.sqrt(tr + 1.0) * 2.0
        w, x, y, z = 0.25 * S, (m21 - m12) / S, (m02 - m20) / S, (m10 - m01) / S
    elif m00 > m11 and m00 > m22:
        S = np.sqrt(1.0 + m00 - m11 - m22) * 2.0
        w, x, y, z = (m21 - m12) / S, 0.25 * S, (m01 + m10) / S, (m02 + m20) / S
    elif m11 > m22:
        S = np.sqrt(1.0 + m11 - m00 - m22) * 2.0
        w, x, y, z = (m02 - m20) / S, (m01 + m10) / S, 0.25 * S, (m12 + m21) / S
    else:
        S = np.sqrt(1.0 + m22 - m00 - m11) * 2.0
        w, x, y, z = (m10 - m01) / S, (m02 + m20) / S, (m12 + m21) / S, 0.25 * S
    q = np.array([x, y, z, w])
    return q / np.linalg.norm(q)


def poses_to_pos_quat(arr):
    """(N,7) TX90 EE pose → position (N,3) + quaternion (N,4). 변환 없음."""
    pos = arr[:, :3].astype(float)
    quat = np.empty((len(arr), 4))
    for i, (rx, ry, rz) in enumerate(arr[:, 3:6]):
        quat[i] = matrix_to_quaternion(euler_xyz_to_matrix(rx, ry, rz))
    return pos, quat


# ══════════════════════════════ predict 모드 ══════════════════════════════
def cmd_predict(args):
    try:
        import torch
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from lerobot.policies.act.modeling_act import ACTPolicy
    except ModuleNotFoundError as e:
        raise SystemExit(
            f"모듈이 없습니다: {e.name}\n"
            "  predict 는 Docker(physical_ai_server) 안에서 실행해야 합니다.")

    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("[경고] CUDA 를 쓸 수 없어 CPU 로 내립니다 (ACT 추론은 CPU 로도 가볍다)")
        device = "cpu"

    print(f"[정책] {args.checkpoint}")
    policy = ACTPolicy.from_pretrained(args.checkpoint)
    policy.to(device).eval()
    policy.reset()
    n_steps = getattr(policy.config, "n_action_steps", "?")
    print(f"  chunk_size={getattr(policy.config, 'chunk_size', '?')}, "
          f"n_action_steps={n_steps}, device={device}")

    print(f"[데이터셋] {args.repo_id}")
    ds = LeRobotDataset(args.repo_id)
    i0 = ds.episode_data_index["from"][args.episode].item()
    i1 = ds.episode_data_index["to"][args.episode].item()
    if args.max_frames:
        i1 = min(i1, i0 + args.max_frames)
    n = i1 - i0
    print(f"  에피소드 {args.episode}: 프레임 {i0}~{i1-1} ({n}개, {n/ds.fps:.1f}초)")

    img_keys = [k for k in ds.meta.features if k.startswith("observation.images")]
    preds, gts, stamps = [], [], []
    t0 = time.time()
    with torch.inference_mode():
        for j, idx in enumerate(range(i0, i1)):
            item = ds[idx]
            batch = {"observation.state":
                     item["observation.state"].unsqueeze(0).to(device)}
            for k in img_keys:
                batch[k] = item[k].unsqueeze(0).to(device)
            a = policy.select_action(batch)
            preds.append(a.squeeze(0).cpu().numpy())
            gts.append(item["action"].numpy())
            stamps.append(float(item["timestamp"]))
            if (j + 1) % 100 == 0 or j + 1 == n:
                el = time.time() - t0
                print(f"  {j+1}/{n}  ({el:.0f}s, {el/(j+1)*1000:.0f} ms/프레임)")

    pred = np.array(preds)
    gt = np.array(gts)

    # ── 정답과 비교 ──
    pos_err = np.linalg.norm(pred[:, :3] - gt[:, :3], axis=1) * 1000  # mm
    ang_err = np.empty(len(pred))
    for i in range(len(pred)):
        Rp = euler_xyz_to_matrix(*pred[i, 3:6])
        Rg = euler_xyz_to_matrix(*gt[i, 3:6])
        c = np.clip((np.trace(Rp.T @ Rg) - 1.0) / 2.0, -1.0, 1.0)
        ang_err[i] = np.degrees(np.arccos(c))
    grip_ok = float(((pred[:, 6] < GRIP_TH) == (gt[:, 6] < GRIP_TH)).mean()) * 100

    print(f"\n[정답 대비]  (open-loop, 관측은 데이터셋 실측)")
    print(f"  위치 오차 : 평균 {pos_err.mean():.1f} mm, 중앙값 {np.median(pos_err):.1f} mm, "
          f"최대 {pos_err.max():.1f} mm")
    print(f"  자세 오차 : 평균 {ang_err.mean():.1f}°, 최대 {ang_err.max():.1f}°")
    print(f"  그리퍼 개폐 일치율: {grip_ok:.1f}% (임계값 {GRIP_TH})")
    if pos_err.mean() > 50:
        print("  [경고] 위치 오차 평균이 5cm 를 넘습니다. 체크포인트 스텝 수가 "
              "낮거나 학습에 문제가 있을 수 있습니다.")

    # ── 저장 ──
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    df = pd.DataFrame({
        "action": list(pred.astype(np.float32)),        # replay 가 읽는 컬럼
        "gt_action": list(gt.astype(np.float32)),
        "timestamp": np.array(stamps, dtype=np.float32),
        "frame_index": np.arange(n),
        "episode_index": args.episode,
    })
    df.to_parquet(args.out, index=False)
    print(f"\n[저장] {args.out}")
    print(f"  다음: python3 run_policy_tx90.py replay --parquet {args.out}")

    if args.plot:
        save_plot(pred, gt, np.array(stamps), args.out, args.episode)


def save_plot(pred, gt, t, out_parquet, ep):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        print("[plot] matplotlib 이 없어 그림은 건너뜁니다")
        return
    fig, axes = plt.subplots(4, 1, figsize=(10, 9), sharex=True)
    for i, (ax, name) in enumerate(zip(axes[:3], "xyz")):
        ax.plot(t, gt[:, i] * 1000, label="GT", lw=1.5)
        ax.plot(t, pred[:, i] * 1000, label="pred", lw=1.0, alpha=0.8)
        ax.set_ylabel(f"{name} (mm)")
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper right")
    axes[0].set_title(f"episode {ep}  policy vs GT")
    axes[3].plot(t, gt[:, 6], label="GT", lw=1.5)
    axes[3].plot(t, pred[:, 6], label="pred", lw=1.0, alpha=0.8)
    axes[3].axhline(GRIP_TH, color="r", ls="--", lw=0.8, label=f"임계 {GRIP_TH}")
    axes[3].set_ylabel("gripper")
    axes[3].set_xlabel("time (s)")
    axes[3].grid(alpha=0.3)
    axes[3].legend(loc="upper right")
    png = os.path.splitext(out_parquet)[0] + ".png"
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    print(f"[plot] {png}")


# ══════════════════════════════ replay 모드 ══════════════════════════════
def dedup_waypoints(pos, quat, min_move=0.0005, min_turn=0.5):
    """정지 구간의 중복 waypoint 제거 — 길이 0 구간은 fraction 만 깎는다."""
    keep = [0]
    for i in range(1, len(pos)):
        j = keep[-1]
        moved = np.linalg.norm(pos[i] - pos[j])
        dot = min(1.0, abs(float(np.dot(quat[i], quat[j]))))
        turned = np.degrees(2.0 * np.arccos(dot))
        if moved > min_move or turned > min_turn:
            keep.append(i)
    return np.array(keep)


def report_workspace(pos, quat):
    print(f"\n[궤적 점검] {len(pos)} waypoint (TX90 base_link, 변환 없이 그대로)")
    for i, n in enumerate("xyz"):
        print(f"  {n}: {pos[:, i].min():+.4f} ~ {pos[:, i].max():+.4f} m")
    d = np.linalg.norm(pos - SHOULDER, axis=1)
    print(f"  어깨거리: {d.min():.3f} ~ {d.max():.3f} m")
    n_far = int((d > MAX_REACH).sum())
    if n_far:
        print(f"  [경고] {n_far}개 waypoint 가 리치 {MAX_REACH} m 초과 — "
              f"정책 출력이 작업영역을 벗어났습니다")
    if pos[:, 2].min() < 0.0:
        print(f"  [경고] z 최소 {pos[:, 2].min():+.4f} m — 바닥 아래")
    approach = 1.0 - 2.0 * (quat[:, 0] ** 2 + quat[:, 1] ** 2)
    down = float((approach < -0.5).mean()) * 100
    print(f"  그리퍼가 아래를 보는 비율: {down:.1f}%")
    if down < 50:
        print("  [경고] 그리퍼가 대부분 아래를 보지 않습니다. 데이터셋이 이미 "
              "tool0 규약이므로 여기서 보정을 추가하면 안 됩니다 — 정책/입력을 확인하세요.")


def report_gripper(grip, t):
    """개폐 전이 시점 로그 — RViz 모델에는 그리퍼가 없어 눈으로 안 보인다."""
    closed = grip < GRIP_TH
    tr = np.flatnonzero(np.diff(closed.astype(int)))
    if len(tr) == 0:
        print("  그리퍼: 개폐 전이 없음")
        return
    ev = ["닫힘" if closed[i + 1] else "열림" for i in tr]
    print("  그리퍼 전이:", ", ".join(
        f"{t[i+1]:.1f}s {e}" for i, e in zip(tr, ev)))


def resolve_home(cli_home):
    if cli_home:
        h = np.array([float(v) for v in cli_home.split(",")])
        if len(h) != 6:
            raise SystemExit("--home 은 쉼표로 구분한 6개 값(rad)이어야 합니다")
        return h
    h = np.array(HOME_JOINTS, dtype=float)
    if HOME_UNIT == "deg":
        h = np.deg2rad(h)
    print(f"[HOME] {np.round(h, 4).tolist()} (rad)")
    return h


class CartesianPlannerNode(Node):
    """v2 의 MoveIt2 연결부 그대로 — 계획, 시간 배분, 50Hz 보간 재생."""

    def __init__(self, home, joint_topic=JOINT_TOPIC):
        super().__init__("run_policy_tx90")
        self.home = list(map(float, home))
        self.cartesian_client = self.create_client(
            GetCartesianPath, "/compute_cartesian_path")
        self.ik_client = self.create_client(GetPositionIK, "/compute_ik")
        self.joint_pub = self.create_publisher(JointState, joint_topic, 10)
        self.pose_pub = self.create_publisher(PoseArray, "/ee_poses_tx90", 10)
        self.display_pub = self.create_publisher(
            DisplayTrajectory, "/display_planned_path", 1)
        self.get_logger().info(f"joint 발행 토픽: {joint_topic}")

    def wait_for_service(self, timeout=10.0):
        self.get_logger().info("MoveIt2 /compute_cartesian_path 대기 중...")
        if not self.cartesian_client.wait_for_service(timeout_sec=timeout):
            self.get_logger().error(
                "서비스를 찾을 수 없습니다. demo.launch.py 가 실행 중인지 확인하세요.")
            return False
        self.get_logger().info("서비스 연결됨")
        return True

    def publish_home(self):
        msg = JointState()
        msg.header = Header()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = JOINT_NAMES
        msg.position = self.home
        self.joint_pub.publish(msg)

    def plan(self, pos, quat, step_size=0.01, jump_threshold=0.0):
        poses = []
        for p, q in zip(pos, quat):
            m = Pose()
            m.position.x, m.position.y, m.position.z = map(float, p)
            m.orientation.x, m.orientation.y, m.orientation.z, m.orientation.w = map(float, q)
            poses.append(m)

        pa = PoseArray()
        pa.header.frame_id = BASE_FRAME
        pa.header.stamp = self.get_clock().now().to_msg()
        pa.poses = poses
        self.pose_pub.publish(pa)

        start_state = RobotState()
        start_state.joint_state.name = JOINT_NAMES
        start_state.joint_state.position = self.home

        req = GetCartesianPath.Request()
        req.header.frame_id = BASE_FRAME
        req.header.stamp = self.get_clock().now().to_msg()
        req.group_name = GROUP_NAME
        req.link_name = EE_LINK
        req.start_state = start_state
        req.waypoints = poses
        req.max_step = step_size
        req.jump_threshold = jump_threshold
        req.avoid_collisions = True

        self.get_logger().info(f"계획 중... waypoint {len(poses)}개")
        future = self.cartesian_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=60.0)
        if future.result() is None:
            self.get_logger().error("서비스 호출 실패")
            return None, 0.0

        res = future.result()
        f = res.fraction
        self.get_logger().info(f"fraction = {f:.3f} ({f*100:.1f}%)")
        if 0.0 < f < 0.999:
            i = min(int(f * len(pos)), len(pos) - 1)
            p = pos[i]
            d = np.linalg.norm(p - SHOULDER)
            self.get_logger().warn(
                f"waypoint {i}/{len(pos)} 부근에서 끊김: "
                f"pos=[{p[0]:+.3f} {p[1]:+.3f} {p[2]:+.3f}] m, 어깨거리 {d:.3f} m")
            if i >= len(pos) - 3:
                self.get_logger().warn("마지막 구간이라 사실상 완주입니다.")
            elif d > MAX_REACH:
                self.get_logger().warn("리치 초과 — 정책 출력이 작업영역을 벗어났습니다.")
            else:
                self.get_logger().warn(
                    "도달 범위 안인데 끊김 — 특이점/급한 자세 전환. "
                    "--downsample 을 낮춰 보세요.")
        return res.solution, f

    def plan_ik(self, pos, quat, times, timeout_ns=200_000_000):
        """waypoint 마다 IK 를 풀어 joint 궤적을 직접 만든다.

        이 작업 자세들은 joint_5 ≈ 0° 손목 특이점 위에 있어서 (2026-08-13 실측),
        compute_cartesian_path 는 밀리미터 차이로 IK 가 갈려 조기에 끊긴다.
        여기서는 각 waypoint 를 직전 해를 시드로 개별로 풀고, 안 풀리는
        waypoint 는 건너뛴다. joint 공간 보간은 특이점을 무리 없이 지난다.
        실기에서도 정책 실행은 이 방식(포즈 → IK → 서보)이 된다.
        """
        def solve(p, q, sd):
            req = GetPositionIK.Request()
            req.ik_request.group_name = GROUP_NAME
            rs = RobotState()
            rs.joint_state.name = JOINT_NAMES
            rs.joint_state.position = list(map(float, sd))
            req.ik_request.robot_state = rs
            ps = PoseStamped()
            ps.header.frame_id = BASE_FRAME
            ps.pose.position.x, ps.pose.position.y, ps.pose.position.z = map(float, p)
            (ps.pose.orientation.x, ps.pose.orientation.y,
             ps.pose.orientation.z, ps.pose.orientation.w) = map(float, q)
            req.ik_request.pose_stamped = ps
            req.ik_request.timeout.nanosec = timeout_ns
            req.ik_request.avoid_collisions = True
            future = self.ik_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
            res = future.result()
            if res is None or res.error_code.val != 1:
                return None
            sol = dict(zip(res.solution.joint_state.name,
                           res.solution.joint_state.position))
            return [sol[n] for n in JOINT_NAMES]

        # IK 실패/branch flip 시 시드를 흔들어 재시도한다. 특이점(joint_5≈0) 근처는
        # 시드에 민감해서, 나쁜 branch 에 한번 들어가면 연쇄 실패한다 (wp34~39 실측).
        PERTURB = [np.zeros(6),
                   np.deg2rad([0, 0, 0, +20, +10, +20]),
                   np.deg2rad([0, 0, 0, -20, +10, -20]),
                   np.deg2rad([0, +5, -5, 0, +15, 0])]

        seed = list(self.home)
        pts, kept, skipped = [], [], []
        jump_warned = 0
        for i, (p, q) in enumerate(zip(pos, quat)):
            j = None
            for d in PERTURB:
                cand = solve(p, q, list(np.array(seed) + d))
                if cand is None:
                    continue
                # 2pi 접기: 한계 안에서 seed 에 가장 가까운 등가각으로 (+361° → +1°)
                for k in range(6):
                    lo, hi = JOINT_LIMITS[k]
                    while cand[k] - seed[k] > np.pi and cand[k] - 2 * np.pi >= lo:
                        cand[k] -= 2 * np.pi
                    while seed[k] - cand[k] > np.pi and cand[k] + 2 * np.pi <= hi:
                        cand[k] += 2 * np.pi
                # 접어도 크게 다르면 branch flip (팔꿈치 뒤집힘 등) → 다음 시드로
                dj = np.degrees(np.abs(np.array(cand) - np.array(seed)))
                if pts and dj.max() > 90:
                    continue
                j = cand
                break
            if j is None:
                if jump_warned < 3:
                    self.get_logger().warn(
                        f"waypoint {i}: 재시도 {len(PERTURB)}회에도 일관된 해 없음 → 건너뜀")
                jump_warned += 1
                skipped.append(i)
                continue
            pts.append(j)
            kept.append(i)
            seed = j
        if skipped:
            self.get_logger().warn(
                f"IK 실패로 건너뜀 {len(skipped)}/{len(pos)}개: "
                f"{skipped[:10]}{' ...' if len(skipped) > 10 else ''}")
        if not pts:
            return None, 0.0

        traj = RobotTrajectory()
        traj.joint_trajectory.joint_names = JOINT_NAMES
        for j, i in zip(pts, kept):
            pt = JointTrajectoryPoint()
            pt.positions = [float(v) for v in j]
            tv = float(times[i])
            pt.time_from_start.sec = int(tv)
            pt.time_from_start.nanosec = int((tv - int(tv)) * 1e9)
            traj.joint_trajectory.points.append(pt)
        frac = len(kept) / len(pos)
        self.get_logger().info(
            f"IK 체인 계획: {len(kept)}/{len(pos)} waypoint 성공 ({frac*100:.1f}%)")
        return traj, frac

    def replay(self, trajectory, grip=None, grip_times=None,
               speed=1.0, duration=None, rate=50.0):
        if trajectory is None:
            return
        points = trajectory.joint_trajectory.points
        names = trajectory.joint_trajectory.joint_names
        if not points:
            self.get_logger().error("trajectory 에 포인트가 없습니다.")
            return

        times = np.array([p.time_from_start.sec + p.time_from_start.nanosec * 1e-9
                          for p in points])
        # compute_cartesian_path 는 시간을 안 채운다 → 원본 길이로 균등 배분
        if times.max() < 1e-6:
            if not duration or duration <= 0:
                duration = len(points) * 0.05
            times = np.linspace(0.0, duration, len(points))
            self.get_logger().info(f"시간 정보 없음 → {duration:.1f}초 균등 배분")

        for pt, tv in zip(points, times):
            pt.time_from_start.sec = int(tv)
            pt.time_from_start.nanosec = int((tv - int(tv)) * 1e9)
        disp = DisplayTrajectory()
        start = RobotState()
        start.joint_state.name = JOINT_NAMES
        start.joint_state.position = self.home
        disp.trajectory_start = start
        disp.trajectory.append(trajectory)
        self.display_pub.publish(disp)

        P = np.array([list(p.positions) for p in points], dtype=float)
        total = float(times[-1]) / speed
        self.get_logger().info(
            f"RViz 재생: {len(points)} 경로점 → {rate:.0f} Hz 보간, {total:.1f}초")

        def publish(q):
            msg = JointState()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.name = names
            msg.position = [float(v) for v in q]
            self.joint_pub.publish(msg)

        # 그리퍼 개폐를 재생 중 로그로 알려준다 (RViz 모델엔 그리퍼가 없다)
        grip_state = None
        if grip is not None and grip_times is not None and len(grip_times):
            grip_state = bool(grip[0] < GRIP_TH)

        dt = 1.0 / rate
        t0 = time.time()
        nxt, step = 0.0, total / 4.0
        while True:
            el = time.time() - t0
            if el >= total:
                break
            tt = el * speed
            publish([np.interp(tt, times, P[:, j]) for j in range(P.shape[1])])
            if grip_state is not None:
                g = float(np.interp(tt, grip_times, grip))
                closed = g < GRIP_TH
                if closed != grip_state:
                    self.get_logger().info(
                        f"  {el:.1f}s 그리퍼 {'닫힘' if closed else '열림'} ({g:.3f})")
                    grip_state = closed
            if el >= nxt:
                self.get_logger().info(f"  {el:.1f}/{total:.1f}s")
                nxt += step
            time.sleep(dt)
        publish(P[-1])
        self.get_logger().info("재생 완료")


def cmd_replay(args):
    df = pd.read_parquet(args.parquet)
    if "episode_index" in df.columns and args.episode is not None:
        sub = df[df["episode_index"] == args.episode]
        if len(sub):
            df = sub
    if args.column not in df.columns:
        raise SystemExit(f"'{args.column}' 컬럼이 없습니다. 있는 컬럼: {list(df.columns)}")
    arr = np.stack(df[args.column].to_numpy()).astype(float)
    print(f"[로드] {args.parquet}")
    print(f"  컬럼 '{args.column}', {len(arr)} 프레임")
    if arr.shape[1] != 7:
        raise SystemExit(f"7차원 EE pose 가 아닙니다: shape {arr.shape}")

    ep_dur = args.duration
    ts = df["timestamp"].to_numpy() if "timestamp" in df.columns else None
    if ep_dur is None and ts is not None:
        ep_dur = float(ts[-1] - ts[0])
        print(f"  원본 길이 {ep_dur:.1f}초")

    full = arr.copy()
    full_t = (ts - ts[0]) if ts is not None else np.arange(len(arr)) / 30.0
    arr = arr[::args.downsample]
    print(f"  다운샘플 1/{args.downsample} → {len(arr)} waypoint")

    # ★ 변환 없음 — 정책 출력이 이미 TX90 base_link + tool0 규약 ★
    pos, quat = poses_to_pos_quat(arr)
    wp_times = full_t[::args.downsample]

    # 특이점 회피 평행이동 (2026-08-13 실험: -0.10,0,0 또는 0,+0.05,0 → 52/52)
    if args.offset:
        off = np.array([float(v) for v in args.offset.split(",")])
        if len(off) != 3:
            raise SystemExit("--offset 은 dx,dy,dz 3개 값(m)이어야 합니다")
        pos = pos + off
        print(f"  평행이동 적용: dx={off[0]:+.3f} dy={off[1]:+.3f} dz={off[2]:+.3f} m")

    if not args.no_dedup:
        keep = dedup_waypoints(pos, quat)
        n_drop = len(pos) - len(keep)
        if n_drop:
            print(f"  정지 중복 제거 {n_drop}개 → {len(keep)} waypoint")
        pos, quat, wp_times = pos[keep], quat[keep], wp_times[keep]

    report_workspace(pos, quat)
    report_gripper(full[:, 6], full_t)

    if args.dry_run:
        print("\n--dry_run 이므로 여기서 종료합니다.")
        return

    if not ROS_OK:
        raise SystemExit(
            f"ROS2 모듈을 찾을 수 없습니다: {ROS_MISSING}\n"
            "  replay 는 Docker(physical_ai_server) 안에서 실행해야 합니다.\n"
            "  host 에서는 --dry_run 으로 점검만 할 수 있습니다.")

    home = resolve_home(args.home)
    rclpy.init()
    node = CartesianPlannerNode(home, joint_topic=args.joint_topic)
    try:
        if not node.wait_for_service():
            return
        node.publish_home()
        time.sleep(1.0)
        if args.planner == "ik":
            trajectory, fraction = node.plan_ik(pos, quat, wp_times)
        else:
            trajectory, fraction = node.plan(
                pos, quat, step_size=args.step_size,
                jump_threshold=args.jump_threshold)
            if 0 < fraction <= 0.3:
                print("  힌트: 이 작업 자세는 손목 특이점(joint_5≈0) 위라 Cartesian "
                      "계획이 잘 끊깁니다. --planner ik 를 써보세요.")
        if fraction > 0.3:
            print(f"\n계획 성공 (fraction={fraction*100:.1f}%) → RViz 재생")
            for r in range(args.repeat):
                if args.repeat > 1:
                    node.get_logger().info(f"── 반복 {r+1}/{args.repeat} ──")
                node.publish_home()
                time.sleep(0.6)
                node.replay(trajectory, grip=full[:, 6], grip_times=full_t,
                            speed=args.speed,
                            duration=(ep_dur or 0) * fraction, rate=args.rate)
        else:
            print(f"\n계획 실패 (fraction={fraction*100:.1f}%)")
            print("  위의 '끊김' 로그와 [궤적 점검] 범위를 먼저 확인하세요.")
    finally:
        rclpy.shutdown()


# ════════════════════════════════ main ════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("predict", help="정책 추론 → 예측 궤적 parquet")
    p.add_argument("--checkpoint", required=True,
                   help=".../checkpoints/<step>/pretrained_model 디렉터리")
    p.add_argument("--episode", type=int, default=0)
    p.add_argument("--repo_id", default=DEFAULT_REPO)
    p.add_argument("--out", default=None, help="예측 parquet 저장 경로")
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    p.add_argument("--max_frames", type=int, default=None)
    p.add_argument("--plot", action="store_true", help="pred vs GT 그림 저장")

    r = sub.add_parser("replay", help="궤적을 MoveIt2 로 계획해 RViz 재생")
    r.add_argument("--parquet", required=True,
                   help="predict 출력 또는 데이터셋 episode parquet")
    r.add_argument("--column", default="action",
                   help="재생할 컬럼 (예측=action, 정답 비교=gt_action)")
    r.add_argument("--planner", default="ik", choices=["ik", "cartesian"],
                   help="ik = waypoint 별 IK (특이점에 강함, 기본) / "
                        "cartesian = compute_cartesian_path")
    r.add_argument("--episode", type=int, default=None)
    r.add_argument("--downsample", type=int, default=10)
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--step_size", type=float, default=0.01)
    r.add_argument("--jump_threshold", type=float, default=0.0)
    r.add_argument("--dry_run", action="store_true", help="ROS 없이 점검만")
    r.add_argument("--home", default=None, help="HOME joint 6개, 쉼표 구분 (rad)")
    r.add_argument("--no_dedup", action="store_true")
    r.add_argument("--duration", type=float, default=None)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--rate", type=float, default=50.0)
    r.add_argument("--joint_topic", default=JOINT_TOPIC)
    r.add_argument("--offset", default=None,
                   help="궤적 평행이동 dx,dy,dz (m, base_link 기준). "
                        "특이점 회피용 — 실기에서는 테이프도 같은 만큼 옮겨야 한다. "
                        "예: --offset=-0.10,0,0")

    args = ap.parse_args()
    if args.mode == "predict":
        if args.out is None:
            args.out = f"/root/policy_rollouts/ep{args.episode:03d}_pred.parquet"
        cmd_predict(args)
    else:
        cmd_replay(args)


if __name__ == "__main__":
    main()
