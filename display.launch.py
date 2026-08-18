import os
from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    # 메쉬 파일 경로 등록 (staubli_tx2_90_support 위치)
    package_path = os.path.abspath('staubli_tx2_90_support/staubli_experimental')
    if 'AMENT_PREFIX_PATH' in os.environ:
        os.environ['AMENT_PREFIX_PATH'] += os.pathsep + package_path
    else:
        os.environ['AMENT_PREFIX_PATH'] = package_path

    with open('tx2_90.urdf', 'r') as f:
        robot_desc = f.read()

    return LaunchDescription([
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{'robot_description': robot_desc}]
        ),
        Node(
            package='joint_state_publisher_gui',
            executable='joint_state_publisher_gui',
            name='joint_state_publisher_gui',
            output='screen'
        ),
        Node(
            package='rviz2',
            executable='rviz2',
            name='rviz2',
            output='screen'
        )
    ])