#!/usr/bin/env python3
"""ee_to_tx90_cartesian_v2.py  —  STEP 5

OMX EE pose (parquet) → Umeyama similarity transform → MoveIt2 Cartesian → RViz

v1 대비 바뀐 것
  1. position   : 손튜닝 affine(SCALE_X=1.0292 …) 을 걷어내고 transform.npy 의 T(s,R,t) 사용
  2. orientation: R 로 회전시킨다. s 는 곱하지 않는다.
  3. tool frame : OMX end_effector_link 는 접근축이 로컬 +X, TX90 tool0 는 로컬 +Z 라
                  90° 보정이 필요하다. 이게 빠지면 그리퍼가 옆을 본다.
                      R_ee = R @ R_omx @ Ry(+90°)
  4. HOME       : all-zero 대신 실측 joint 값에서 출발 (branch switching 대응)
  5. 진단       : 계획이 어느 waypoint 에서 끊겼는지, 도달 범위를 벗어났는지 출력

사용법 (Docker 안, demo.launch.py 먼저 실행)
  python3 ee_to_tx90_cartesian_v2.py \
      --parquet /root/omx_act_pick_and_place_v4_162_ee/data/chunk-000/episode_000000.parquet \
      --episode 0 --downsample 10
"""

import argparse
import os
import time

import numpy as np
import pandas as pd

# ROS2 는 --dry_run 에 필요 없다. host 에 moveit_msgs 가 없어도 변환 결과를 확인할 수
# 있도록, import 실패를 여기서 삼키고 실제로 계획을 돌릴 때만 막는다.
try:
    import rclpy
    from rclpy.node import Node
    from geometry_msgs.msg import Pose, PoseArray
    from moveit_msgs.msg import RobotState, RobotTrajectory, DisplayTrajectory
    from moveit_msgs.srv import GetCartesianPath
    from sensor_msgs.msg import JointState
    from std_msgs.msg import Header
    ROS_OK, ROS_MISSING = True, None
except ModuleNotFoundError as _e:
    ROS_OK, ROS_MISSING = False, _e.name

    class Node:            # --dry_run 전용 더미. 인스턴스화되지 않는다.
        pass

    RobotTrajectory = object


def require_ros():
    if not ROS_OK:
        raise SystemExit(
            f"ROS2 모듈을 찾을 수 없습니다: {ROS_MISSING}\n"
            "  계획·재생은 Docker(physical_ai_server) 안에서 실행해야 합니다.\n"
            "    cd ~/physical_ai_tools/docker && ./container.sh enter\n"
            "  host 에서는 --dry_run 으로 변환 결과만 확인할 수 있습니다.")

# ═════════════════════════════════ 설정 ═════════════════════════════════
JOINT_NAMES = ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6"]

# ★ STEP 3-4 에서 pendant 로 잰 HOME 자세를 채우세요 ★
#   작업영역 위 ~30cm, 로봇이 편하게 대기하는 자세의 joint 6개.
#   단위를 반드시 맞출 것 — pendant 가 도(deg) 표시면 HOME_UNIT = "deg".
# 펜던트 joint 규약이 URDF 와 동일한 것은 확인됐다. 측정한 5개 자세의 joint 를
# URDF 대로 FK 했더니 X, Y 가 0.1mm 이내로 일치했다. 부호·영점 보정 없이 deg → rad 만 하면 된다.
HOME_UNIT = "deg"                       # "rad" 또는 "deg"
HOME_JOINTS = [-3.31, 56.92, 73.58, -1.04, 48.85, -47.38]   # 2026-08-12 실측

# 그리퍼 미장착 → MoveIt 이 움직일 링크는 맨 flange 인 tool0 그대로.
# 나중에 그리퍼를 달면 URDF 에 tool link 를 추가하고 이 이름만 바꾸면 된다.
# transform.npy 는 그대로 유효하다.
EE_LINK = "tool0"
GROUP_NAME = "tx2_90_arm"
BASE_FRAME = "base_link"

# demo.launch.py 는 joint_state_publisher 를 source_list=["/move_group/fake_controller_joint_states"]
# 로 띄운다. 그 노드가 /joint_states 를 계속 발행하므로, 우리가 /joint_states 에 직접 쏘면
# 둘이 번갈아 나가면서 화면이 자세와 all-zero 사이를 오간다.
# source_list 토픽에 쏘면 joint_state_publisher 가 받아서 중계하므로 발행자가 하나로 유지된다.
JOINT_TOPIC = "/move_group/fake_controller_joint_states"

# TX2-90 도달 범위 점검용 (base_link 기준 어깨 위치와 최대 리치)
SHOULDER = np.array([0.0, 0.0, 0.478])
MAX_REACH = 0.95                        # m, 여유를 둔 값
# ════════════════════════════════════════════════════════════════════════


def rot_y(theta):
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


# OMX end_effector_link(+X 접근) → TX90 tool0(+Z 접근)
R_TOOL = rot_y(np.pi / 2)


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
    """회전행렬 → 쿼터니언 [x, y, z, w]. Shepperd 방식이라 수치적으로 안정적이다."""
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


def load_transform(path):
    d = np.load(path, allow_pickle=True).item()
    s, R, t = float(d["s"]), np.asarray(d["R"]), np.asarray(d["t"])
    print(f"[transform] {path}")
    print(f"  s = {s:.6f}")
    print(f"  t = [{t[0]:+.4f}, {t[1]:+.4f}, {t[2]:+.4f}] m")
    if "residual_mean_mm" in d:
        print(f"  캘리브레이션 residual: mean {d['residual_mean_mm']*1000:.2f} mm "
              f"({d.get('n_points', '?')}점)")
    return s, R, t


def transform_waypoints(arr, s, R, t, tool_fix=True):
    """OMX (N,7) → TX90 position (N,3) + quaternion (N,4)."""
    pos = (s * (R @ arr[:, :3].T).T) + t
    quat = np.empty((len(arr), 4))
    for i, (rx, ry, rz) in enumerate(arr[:, 3:6]):
        R_ee = R @ euler_xyz_to_matrix(rx, ry, rz)
        if tool_fix:
            R_ee = R_ee @ R_TOOL           # s 는 곱하지 않는다
        quat[i] = matrix_to_quaternion(R_ee)
    return pos, quat


def dedup_waypoints(pos, quat, min_move=0.0005, min_turn=0.5):
    """연속으로 같은 자리에 머무는 waypoint 를 걸러낸다.

    시연 데이터는 시작·끝에 로봇이 가만히 있는 구간이 있다(에피소드 0 은 47 개 중 9 개).
    그대로 넘기면 compute_cartesian_path 가 길이 0 인 구간에서 진행을 못 해
    fraction 이 깎인다 — 실제로 못 간 게 아니라 갈 거리가 없는 것이다.
    """
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
    """변환 결과가 TX90 작업영역 안인지, 그리퍼가 아래를 보는지 확인."""
    print(f"\n[변환 결과] {len(pos)} waypoint")
    for i, n in enumerate("xyz"):
        print(f"  {n}: {pos[:, i].min():+.4f} ~ {pos[:, i].max():+.4f} m")

    d = np.linalg.norm(pos - SHOULDER, axis=1)
    print(f"  어깨(base_link z={SHOULDER[2]}) 로부터 거리: "
          f"{d.min():.3f} ~ {d.max():.3f} m")
    n_far = int((d > MAX_REACH).sum())
    if n_far:
        print(f"  [경고] {n_far}개 waypoint 가 리치 {MAX_REACH} m 를 넘습니다. "
              f"TX90 사각형을 팔 앞쪽으로 옮기고 재측정(STEP 3) 하세요.")
    if pos[:, 2].min() < 0.0:
        print(f"  [경고] z 최소값이 {pos[:, 2].min():+.4f} m 로 바닥 아래입니다. "
              f"테이블 높이 측정을 확인하세요.")

    # tool0 의 접근축(로컬 +Z)이 월드에서 어디를 보는지.
    # R @ [0,0,1] 의 z 성분은 1 - 2(x^2 + y^2).
    approach = 1.0 - 2.0 * (quat[:, 0] ** 2 + quat[:, 1] ** 2)
    down = float((approach < -0.5).mean()) * 100
    print(f"  그리퍼가 아래를 보는 비율: {down:.1f}% "
          f"(접근축 z성분 평균 {approach.mean():+.3f})")
    if down < 50:
        print(f"  [경고] 그리퍼가 대부분 아래를 보지 않습니다. "
              f"--no_tool_fix 를 켜고 껐을 때를 비교해 보세요.")


class CartesianPlannerNode(Node):
    def __init__(self, home, joint_topic=JOINT_TOPIC):
        super().__init__("ee_to_tx90_cartesian_v2")
        self.home = list(map(float, home))
        self.cartesian_client = self.create_client(
            GetCartesianPath, "/compute_cartesian_path")
        self.joint_pub = self.create_publisher(JointState, joint_topic, 10)
        self.pose_pub = self.create_publisher(PoseArray, "/ee_poses_tx90", 10)
        self.display_pub = self.create_publisher(
            DisplayTrajectory, "/display_planned_path", 1)
        self.get_logger().info(f"joint 발행 토픽: {joint_topic}")
        self.get_logger().info("초기화 완료")

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

        self.get_logger().info(f"계획 중... waypoint {len(poses)}개, "
                               f"시작 자세 {np.round(self.home, 3).tolist()}")
        future = self.cartesian_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=60.0)

        if future.result() is None:
            self.get_logger().error("서비스 호출 실패")
            return None, 0.0

        res = future.result()
        f = res.fraction
        self.get_logger().info(f"fraction = {f:.3f} ({f*100:.1f}%)")

        # 어디서 끊겼는지 알려준다 — v1 에는 없던 진단
        if 0.0 < f < 0.999:
            i = min(int(f * len(pos)), len(pos) - 1)
            p = pos[i]
            d = np.linalg.norm(p - SHOULDER)
            self.get_logger().warn(
                f"waypoint {i}/{len(pos)} 부근에서 끊김: "
                f"pos=[{p[0]:+.3f} {p[1]:+.3f} {p[2]:+.3f}] m, 어깨거리 {d:.3f} m")
            if i >= len(pos) - 3:
                self.get_logger().warn(
                    "마지막 구간입니다. 시연 끝에서 HOME 으로 돌아가 멈추는 부분이라 "
                    "동작 자체는 사실상 완료된 것입니다. 이 정도 손실은 정상입니다.")
            elif d > MAX_REACH:
                self.get_logger().warn(
                    f"어깨거리 {d:.3f} m 로 리치 {MAX_REACH} m 를 넘습니다. "
                    "TX90 사각형을 팔 앞쪽으로 옮기고 재측정(STEP 3) 하세요.")
            else:
                self.get_logger().warn(
                    "도달 범위 안인데 끊겼습니다. 특이점 근처이거나 자세 전환이 급한 "
                    "구간입니다. --downsample 을 낮춰(3~5) waypoint 를 촘촘히 해보세요.")

        return res.solution, f

    def replay(self, trajectory: RobotTrajectory, speed=1.0, duration=None, rate=50.0):
        if trajectory is None:
            self.get_logger().error("trajectory 가 없습니다.")
            return
        points = trajectory.joint_trajectory.points
        names = trajectory.joint_trajectory.joint_names
        if not points:
            self.get_logger().error("trajectory 에 포인트가 없습니다.")
            return

        times = np.array([p.time_from_start.sec + p.time_from_start.nanosec * 1e-9
                          for p in points])
        # compute_cartesian_path 는 경로만 돌려주고 시간을 안 채운다(전부 0).
        # 그대로 재생하면 164 포인트가 몇 밀리초에 다 지나가서 "고개 한 번 까닥"으로 보인다.
        if times.max() < 1e-6:
            if not duration or duration <= 0:
                duration = len(points) * 0.05
            times = np.linspace(0.0, duration, len(points))
            self.get_logger().warn(
                f"trajectory 에 시간 정보가 없어 {duration:.1f}초로 균등 배분했습니다 "
                f"(compute_cartesian_path 는 시간을 채우지 않습니다).")

        # 시간을 trajectory 에 실제로 써넣고 RViz 의 Trajectory 디스플레이에도 보낸다.
        # /display_planned_path 는 joint_states 와 무관한 경로라 충돌하지 않는다.
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

        # 경로점을 그대로 뿌리면 158 포인트 / 14.8 초 = 10.7 Hz 라 화면이 뚝뚝 끊긴다.
        # 실시간 시계를 보면서 joint 를 선형보간해 rate Hz 로 발행한다.
        P = np.array([list(p.positions) for p in points], dtype=float)
        total = float(times[-1]) / speed
        self.get_logger().info(
            f"RViz 재생 시작: {len(points)} 경로점 → {rate:.0f} Hz 보간, {total:.1f}초")

        def publish(q):
            msg = JointState()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.name = names
            msg.position = [float(v) for v in q]
            self.joint_pub.publish(msg)

        dt = 1.0 / rate
        t0 = time.time()
        nxt, step = 0.0, total / 4.0
        while True:
            el = time.time() - t0
            if el >= total:
                break
            tt = el * speed
            publish([np.interp(tt, times, P[:, j]) for j in range(P.shape[1])])
            if el >= nxt:
                self.get_logger().info(f"  {el:.1f}/{total:.1f}s")
                nxt += step
            time.sleep(dt)
        publish(P[-1])
        self.get_logger().info("재생 완료")

    def replay_loop(self, trajectory, speed=1.0, duration=None, times=1, rate=50.0):
        for n in range(times):
            if times > 1:
                self.get_logger().info(f"── 반복 {n+1}/{times} ──")
            self.publish_home()
            time.sleep(0.6)
            self.replay(trajectory, speed=speed, duration=duration, rate=rate)


def resolve_home(cli_home):
    if cli_home:
        h = np.array([float(v) for v in cli_home.split(",")])
        if len(h) != 6:
            raise SystemExit("--home 은 쉼표로 구분한 6개 값이어야 합니다")
        print(f"[HOME] --home 으로 지정: {np.round(h, 4).tolist()} (rad)")
        return h

    h = np.array(HOME_JOINTS, dtype=float)
    if np.isnan(h).any():
        raise SystemExit(
            "HOME_JOINTS 가 아직 비어 있습니다.\n"
            "  STEP 3-4 에서 pendant 로 잰 HOME 자세 joint 6개를 이 파일 상단에 채우거나,\n"
            "  --home 0,-0.35,1.2,0,0.75,0 처럼 넘기세요.\n"
            "  all-zero 로 두면 v1 과 같은 branch switching 문제가 그대로 재현됩니다.")
    if HOME_UNIT == "deg":
        h = np.deg2rad(h)
        print(f"[HOME] deg → rad 변환 적용")
    print(f"[HOME] {np.round(h, 4).tolist()} (rad)")
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--episode", type=int, default=0)
    ap.add_argument("--downsample", type=int, default=10)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--column", default="action", choices=["action", "observation.state"])
    ap.add_argument("--transform", default=os.path.expanduser(
        "~/omx_workspace/transform.npy"))
    ap.add_argument("--step_size", type=float, default=0.01)
    ap.add_argument("--jump_threshold", type=float, default=0.0)
    ap.add_argument("--no_tool_fix", action="store_true",
                    help="tool frame 90° 보정을 끈다 (증상 비교용)")
    ap.add_argument("--dry_run", action="store_true",
                    help="ROS2 없이 변환 결과만 확인")
    ap.add_argument("--home", default=None, help="HOME joint 6개, 쉼표 구분 (rad)")
    ap.add_argument("--no_dedup", action="store_true",
                    help="정지 중복 waypoint 제거를 끈다")
    ap.add_argument("--duration", type=float, default=None,
                    help="재생 시간(초). 기본은 원본 에피소드 길이")
    ap.add_argument("--repeat", type=int, default=1, help="재생 반복 횟수")
    ap.add_argument("--rate", type=float, default=50.0,
                    help="RViz 발행 주기 Hz. 낮으면 화면이 끊겨 보인다")
    ap.add_argument("--joint_topic", default=JOINT_TOPIC,
                    help="joint 발행 토픽. /joint_states 에 직접 쏘면 "
                         "joint_state_publisher 와 충돌해 all-zero 로 튄다")
    args = ap.parse_args()

    s, R, t = load_transform(args.transform)

    df = pd.read_parquet(args.parquet)
    if "episode_index" in df.columns and args.episode is not None:
        sub = df[df["episode_index"] == args.episode]
        if len(sub):
            df = sub
    arr = np.stack(df[args.column].to_numpy())
    print(f"\n[로드] {args.parquet}")
    print(f"  에피소드 {args.episode}, 컬럼 '{args.column}', {len(arr)} 프레임")

    # 원본 재생 시간 — compute_cartesian_path 가 시간을 안 채우므로 여기서 가져온다
    ep_dur = args.duration
    if ep_dur is None and "timestamp" in df.columns:
        ep_dur = float(df["timestamp"].iloc[-1] - df["timestamp"].iloc[0])
        print(f"  원본 길이 {ep_dur:.1f}초")

    arr = arr[::args.downsample]
    print(f"  다운샘플 1/{args.downsample} → {len(arr)} waypoint")

    tool_fix = not args.no_tool_fix
    if not tool_fix:
        print("\n  [주의] tool frame 90° 보정이 꺼져 있습니다 (--no_tool_fix). "
              "그리퍼가 옆을 볼 것입니다.")
    pos, quat = transform_waypoints(arr, s, R, t, tool_fix=tool_fix)

    if not args.no_dedup:
        keep = dedup_waypoints(pos, quat)
        n_drop = len(pos) - len(keep)
        if n_drop:
            print(f"  정지 중복 제거 {n_drop}개 → {len(keep)} waypoint "
                  f"(로봇이 가만히 있는 구간이라 경로 길이가 0)")
        pos, quat = pos[keep], quat[keep]

    report_workspace(pos, quat)

    if args.dry_run:
        print("\n--dry_run 이므로 여기서 종료합니다.")
        return

    require_ros()
    home = resolve_home(args.home)

    rclpy.init()
    node = CartesianPlannerNode(home, joint_topic=args.joint_topic)
    try:
        if not node.wait_for_service():
            return
        node.publish_home()
        time.sleep(1.0)

        trajectory, fraction = node.plan(
            pos, quat, step_size=args.step_size, jump_threshold=args.jump_threshold)

        if fraction > 0.3:
            print(f"\n계획 성공 (fraction={fraction*100:.1f}%) → RViz 재생")
            node.replay_loop(trajectory, speed=args.speed,
                             duration=(ep_dur or 0) * fraction,
                             times=args.repeat, rate=args.rate)
        else:
            print(f"\n계획 실패 (fraction={fraction*100:.1f}%)")
            print("  위의 '끊김' 로그와 [변환 결과] 범위를 먼저 확인하세요.")
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    main()
